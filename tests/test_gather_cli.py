import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

from agentree import cli, config, gather as gmod, state
from agentree.model.sample import sample_raw, sample_snapshot
from agentree.model.snapshot import from_dict
from agentree.types import Snapshot, SourceWarning

NOW = 1_800_000_000.0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Env(unittest.TestCase):
    """Isolated AGENTREE_STATE_DIR / AGENTREE_CONFIG for every test (never the real ~/.local/state/agentree)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.env = mock.patch.dict(os.environ, {
            "AGENTREE_STATE_DIR": self.state_dir,
            "AGENTREE_CONFIG": os.path.join(self.tmp.name, "absent.toml"),
        })
        self.env.start()
        self.release = threading.Event()
        gmod._inflight.clear()   # jobs abandoned by earlier tests must not leak in

    def tearDown(self):
        self.release.set()   # let "hanging" fake sources finish
        self.env.stop()
        self.tmp.cleanup()

    def hang(self, *a, **kw):
        self.release.wait(10)
        return (), []


class GatherTest(_Env):
    def setUp(self):
        super().setUp()
        self.raw = sample_raw(NOW)
        self.cfg = config.Config(source_timeout_s=0.2, git_budget_s=0.3)
        self.patch(claude=lambda *a, **kw: ((), []))     # never the real ps / ~/.claude

    def patch(self, **fns):
        # agent providers' module-level fetch lives in agentree.providers.<id>
        ps = [mock.patch(f"agentree.{'providers' if name in ('opencode', 'claude') else 'sources'}.{name}.fetch", fn)
              for name, fn in fns.items()]
        for p in ps:
            p.start()
            self.addCleanup(p.stop)

    def test_raise_and_hang_degrade_to_warnings(self):
        def boom(*a, **kw):
            raise RuntimeError("kaboom")

        seen = {}

        def kitty_fetch(names, timeout, socket_glob=None):
            seen["names"] = sorted(names)
            seen["timeout"] = timeout
            return self.raw["hosts"], [SourceWarning("kitty", "one socket down")]

        def git_fetch(globs, timeout, budget_s):
            seen["budget"] = budget_s
            return self.raw["repos"], []

        self.patch(zellij=lambda *a, **kw: (self.raw["sessions"], []), git=git_fetch,
                   opencode=self.hang, kitty=kitty_fetch)
        t0 = time.monotonic()
        snap = gmod.gather(self.cfg, now=NOW)
        wall = time.monotonic() - t0
        # opencode deadline = 3 * 0.2 + slack; git = 0.3 + slack
        self.assertLess(wall, 3 * 0.2 + gmod.SLACK_S + 0.5)
        self.assertGreaterEqual(wall, 3 * 0.2)
        self.assertEqual(seen["budget"], 0.3)
        self.assertEqual(seen["timeout"], 0.2)
        live = sorted(s.name for s in self.raw["sessions"] if s.state != "exited")
        self.assertEqual(seen["names"], live)
        srcs = {w.source: w.message for w in snap.warnings}
        self.assertIn("timed out", srcs["opencode"])
        self.assertEqual(srcs["kitty"], "one socket down")
        self.assertEqual(snap.taken_at, NOW)
        self.assertFalse(snap.stale)
        names = [s.session.name for s in snap.sessions]
        self.assertIn("web-app", names)
        self.assertTrue(any(s.kitty for s in snap.sessions))
        # no agents (opencode hung) but repos/worktrees still linked
        self.assertEqual(sum(len(w.agents) for s in snap.sessions for w in s.worktrees), 0)
        self.assertTrue(any(s.worktrees for s in snap.sessions))
        # cache written to the temp state dir
        cached = state.load_cache()
        self.assertEqual(replace(cached, stale=False), snap)

        self.patch(git=boom)
        snap = gmod.gather(self.cfg, now=NOW)
        srcs = {w.source: w.message for w in snap.warnings}
        self.assertIn("RuntimeError: kaboom", srcs["git"])
        # opencode's first invocation is still hung: not relaunched, reported instead
        self.assertEqual(srcs["opencode"], "previous refresh still running")
        self.assertEqual(gmod.inflight(), ["opencode"])

    def test_overruns_do_not_multiply_threads(self):
        calls = {"opencode": 0, "git": 0}

        def hang_oc(*a, **kw):
            calls["opencode"] += 1
            return self.hang()

        def hang_git(*a, **kw):
            calls["git"] += 1
            return self.hang()

        self.patch(zellij=lambda *a, **kw: (self.raw["sessions"], []), git=hang_git, opencode=hang_oc,
                   kitty=lambda *a, **kw: ((), []))
        base = threading.active_count()
        counts = []
        for _ in range(6):
            t0 = time.monotonic()
            snap = gmod.gather(self.cfg, now=NOW)
            self.assertLess(time.monotonic() - t0, 3 * 0.2 + gmod.SLACK_S + 0.5)
            self.assertTrue(snap.sessions)
            counts.append(threading.active_count())
        self.assertEqual(calls, {"opencode": 1, "git": 1})
        self.assertLessEqual(max(counts), base + 2)      # the two hung source threads, nothing more
        self.release.set()
        deadline = time.monotonic() + 2
        while gmod.inflight() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(gmod.inflight(), [])
        self.release.clear()
        gmod.gather(self.cfg, now=NOW)                    # relaunched once finished
        self.assertEqual(calls, {"opencode": 2, "git": 2})

    def test_zellij_failure_skips_kitty(self):
        kitty = mock.Mock(return_value=((), []))
        self.patch(zellij=self.hang, git=lambda *a, **kw: ((), []),
                   opencode=lambda *a, **kw: (self.raw["agents"], []), kitty=kitty)
        t0 = time.monotonic()
        snap = gmod.gather(self.cfg, now=NOW)
        self.assertLess(time.monotonic() - t0, 3 * 0.2 + gmod.SLACK_S + 0.5)
        kitty.assert_not_called()
        self.assertEqual(snap.sessions, ())
        self.assertIn("zellij", {w.source for w in snap.warnings})
        self.assertTrue(snap.orphans.agents)   # agents survive without sessions

    def test_opencode_gets_zellij_pane_titles(self):
        got = {}

        def oc_fetch(timeout, pane_titles=None):
            got["titles"] = pane_titles()
            return (), []

        self.patch(zellij=lambda *a, **kw: (self.raw["sessions"], []), git=lambda *a, **kw: ((), []),
                   opencode=oc_fetch, kitty=lambda *a, **kw: ((), []))
        gmod.gather(self.cfg, now=NOW)
        want = {(s.name, p.pane_id): p.title for s in self.raw["sessions"] if s.state != "exited"
                for t in s.tabs for p in t.panes if not p.is_plugin and p.title}
        self.assertTrue(want)
        self.assertEqual(got["titles"], want)

    def test_kitty_gets_socket_glob(self):
        seen = {}

        def kitty_fetch(names, timeout, socket_glob=None):
            seen["glob"] = socket_glob
            return (), []

        self.patch(zellij=lambda *a, **kw: (self.raw["sessions"], []), git=lambda *a, **kw: ((), []),
                   opencode=lambda *a, **kw: ((), []), kitty=kitty_fetch)
        gmod.gather(replace(self.cfg, kitty_socket_glob="/tmp/kitty.sock-9*"), now=NOW)
        self.assertEqual(seen["glob"], "/tmp/kitty.sock-9*")

    def test_opencode_and_claude_agents_merged(self):
        oc_agents = tuple(a for a in self.raw["agents"] if a.provider == "opencode")
        cc_agents = tuple(a for a in self.raw["agents"] if a.provider == "claude")
        self.assertTrue(oc_agents and cc_agents)
        dup = replace(oc_agents[0], provider="claude")            # same pid from both: first (opencode) wins
        self.patch(zellij=lambda *a, **kw: (self.raw["sessions"], []), git=lambda *a, **kw: (self.raw["repos"], []),
                   opencode=lambda *a, **kw: (oc_agents, [SourceWarning("opencode", "o")]),
                   claude=lambda *a, **kw: (cc_agents + (dup,), [SourceWarning("claude", "c")]),
                   kitty=lambda *a, **kw: ((), []))
        snap = gmod.gather(self.cfg, now=NOW)
        got = {a.pid: a.provider for a in gmod.snapshot_agents(snap)}
        self.assertEqual(sorted(got), sorted(a.pid for a in self.raw["agents"]))
        self.assertEqual(got[oc_agents[0].pid], "opencode")
        self.assertEqual({p for p, k in got.items() if k == "claude"}, {a.pid for a in cc_agents})
        self.assertEqual([(w.source, w.message) for w in snap.warnings if w.source in ("opencode", "claude")],
                         [("opencode", "o"), ("claude", "c")])
        self.assertIn("claude", gmod.TIMINGS)
        # the claude agent is placed under its worktree like an opencode agent
        cc = cc_agents[0]
        placed = [(n.session.name, w.worktree.path) for n in snap.sessions for w in n.worktrees
                  if any(a.pid == cc.pid for a in w.agents)]
        self.assertEqual(placed, [(cc.session_name, cc.cwd)])

    def test_claude_failure_is_a_warning(self):
        def boom(*a, **kw):
            raise RuntimeError("cc down")
        self.patch(zellij=lambda *a, **kw: (self.raw["sessions"], []), git=lambda *a, **kw: ((), []),
                   opencode=lambda *a, **kw: (self.raw["agents"][:1], []), claude=boom,
                   kitty=lambda *a, **kw: ((), []))
        snap = gmod.gather(self.cfg, now=NOW)
        self.assertIn("cc down", {w.source: w.message for w in snap.warnings}["claude"])
        self.assertEqual(len(gmod.snapshot_agents(snap)), 1)

    def test_prune_at_most_hourly(self):
        from agentree.providers import claude as ccmod                 # housekeeping lives in the claude provider
        calls = []
        with mock.patch.object(ccmod, "prune", lambda *a, **kw: calls.append(1)), \
                mock.patch.object(ccmod, "_last_prune", None):
            self.assertTrue(ccmod.maybe_prune(now=100.0))
            self.assertFalse(ccmod.maybe_prune(now=100.0 + ccmod.PRUNE_EVERY_S - 1))
            self.assertTrue(ccmod.maybe_prune(now=100.0 + ccmod.PRUNE_EVERY_S))
        self.assertEqual(len(calls), 2)
        with mock.patch.object(ccmod, "prune", side_effect=OSError("ro")), mock.patch.object(ccmod, "_last_prune", None):
            self.assertTrue(ccmod.maybe_prune(now=1.0))           # never raises
        # a full gather runs it (via the provider's fetch_full); a quick refresh does not
        with mock.patch.object(ccmod, "maybe_prune", lambda *a, **kw: calls.append("full")), \
                mock.patch.object(ccmod, "fetch", lambda *a, **kw: ((), [])):
            ccmod.ClaudeProvider(ccmod_spec()).fetch_full(_ctx())
            ccmod.ClaudeProvider(ccmod_spec()).fetch_quick([], _ctx())
        self.assertEqual(calls[2:], ["full"])

    def test_bad_result_and_config_warnings(self):
        self.patch(zellij=lambda *a, **kw: None, git=lambda *a, **kw: ((), []),
                   opencode=lambda *a, **kw: ((), []), kitty=lambda *a, **kw: ((), []))
        cfg = replace(self.cfg, warnings=("unknown config key 'x'",))
        snap = gmod.gather(cfg, now=NOW)
        srcs = {w.source: w.message for w in snap.warnings}
        self.assertIn("bad fetch() result", srcs["zellij"])
        self.assertIn("unknown config key", srcs["config"])
        self.assertIn("total", gmod.TIMINGS)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.run(argv)
    return code, out.getvalue(), err.getvalue()


class _Tty:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


class CliTest(_Env):
    def test_json_cached_round_trip(self):
        snap = sample_snapshot(NOW)
        state.save_cache(snap)
        code, out, _ = run_cli(["--json", "--cached"])
        self.assertEqual(code, 0)
        back = from_dict(json.loads(out))
        self.assertTrue(back.stale)
        self.assertEqual(replace(back, stale=False), snap)

    def test_json_cached_missing(self):
        code, out, err = run_cli(["--json", "--cached"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no cached snapshot", err)

    def test_json_gathers(self):
        snap = sample_snapshot(NOW)
        with mock.patch("agentree.gather.gather", return_value=snap) as g:
            code, out, _ = run_cli(["--json"])
        g.assert_called_once()
        self.assertEqual(from_dict(json.loads(out)), snap)

    def test_tree_demo(self):
        code, out, _ = run_cli(["tree", "--demo"])
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("▾ ● web-app"))
        self.assertIn("busy 2m", out)
        self.assertIn("Needs a home", out)
        self.assertNotIn("▪", out)                  # panes hidden by default
        code, out2, _ = run_cli(["--demo", "tree", "--panes", "--expand"])
        self.assertIn("▪", out2)
        self.assertNotIn("▸", out2)
        code, out3, _ = run_cli(["tree", "--demo", "--text", "dotfiles"])
        self.assertIn("dotfiles", out3)
        self.assertNotIn("web-app", out3)
        code, out4, _ = run_cli(["tree", "--demo", "--mode", "hide"])
        self.assertNotIn("⎇ fix/flaky-e2e", out4)

    def test_flags(self):
        code, _, err = run_cli(["--cached"])
        self.assertEqual(code, 2)

    def test_doctor_wired(self):
        seen = {}

        def fake_doctor(cfg, *, out):
            seen["cfg"] = cfg
            out.write("agentree doctor\n  ✗ FAIL  x\n")
            return 1
        with mock.patch("agentree.doctor.run_doctor", fake_doctor):
            code, out, err = run_cli(["doctor"])
        self.assertEqual(code, 1)
        self.assertIn("✗ FAIL", out)
        self.assertNotIn("not implemented", out + err)
        self.assertIsInstance(seen["cfg"], config.Config)
        with mock.patch("agentree.doctor.run_doctor", lambda cfg, *, out: 0):
            self.assertEqual(run_cli(["doctor"])[0], 0)
        self.assertNotIn("not implemented", cli._parser().format_help())

    def test_tui_needs_tty(self):
        with mock.patch.object(sys, "stdin", _Tty(False)), mock.patch.object(sys, "stdout", _Tty(False)):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.run([]), 1)
        self.assertIn("needs a terminal", err.getvalue())

    def _run_tui(self, argv):
        got = {}

        def fake_run_app(**kw):
            got.update(kw)
            return 0

        self._mods_before = set(sys.modules)
        # hermetic: outside zellij (single-instance is tested separately); the suite may run in a zellij pane
        clean_env = {k: v for k, v in os.environ.items()
                     if k not in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")}
        with mock.patch.dict(os.environ, clean_env, clear=True), \
                mock.patch.object(sys, "stdin", _Tty(True)), mock.patch.object(sys, "stdout", _Tty(True)), \
                mock.patch("agentree.ui.app.run_app", fake_run_app), \
                mock.patch("agentree.gather.gather", side_effect=AssertionError("gathered before first frame")) as g:
            self.assertEqual(cli.run(argv), 0)
            self.assertFalse(g.called)
        return got, g

    def test_tui_uses_cache_and_never_gathers_first(self):
        state.save_cache(sample_snapshot(NOW))
        got, _ = self._run_tui([])
        self.assertTrue(got["snapshot"].stale)
        self.assertEqual(got["snapshot"].taken_at, NOW)
        self.assertEqual(got["save_ui"], state.save_ui)
        self.assertFalse(got["actions"].dry_run)
        self.assertIsNone(got["on_first_frame"])
        self.assertIsInstance(got["live"], gmod.LiveStatus)
        self.assertNotIn("agentree.providers._opencode_events", sys.modules.keys() - self._mods_before)  # lazy: after first frame
        with mock.patch("agentree.gather.gather", return_value=Snapshot(taken_at=5.0)) as g:
            self.assertEqual(got["refresh"]().taken_at, 5.0)
        g.assert_called_once()

    def test_tui_live_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"AGENTREE_LIVE": "0"}):
            got, _ = self._run_tui([])
        self.assertIsNone(got["live"])
        got, _ = self._run_tui(["--demo"])
        self.assertIsNone(got["live"])                       # demo data: nothing to watch

    def test_demo_uses_null_actions_and_runs_nothing(self):
        from agentree.actions.api import NullActions
        from agentree.model.tree import build_view
        from agentree.types import UiState

        got, _ = self._run_tui(["--demo"])
        actions = got["actions"]
        self.assertIsInstance(actions, NullActions)
        snap = got["snapshot"]
        view = build_view(snap, UiState(), snap.taken_at)
        with mock.patch("agentree.actions.exec.run", side_effect=AssertionError("command executed")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process spawned")):
            for r in view.rows:
                if r.selectable:
                    self.assertFalse(actions.jump(r, snap).ok)          # Enter
                    self.assertFalse(actions.open_tab(r, snap).ok)      # t
        got, _ = self._run_tui(["--demo", "--dry-run"])
        self.assertIsInstance(got["actions"], NullActions)

    def test_tui_without_cache_and_flags(self):
        got, _ = self._run_tui(["--dry-run"])
        self.assertEqual(got["snapshot"].sessions, ())
        self.assertTrue(got["snapshot"].stale)
        self.assertTrue(got["actions"].dry_run)
        got, _ = self._run_tui(["--demo"])
        self.assertIn("web-app", [s.session.name for s in got["snapshot"].sessions])
        before = got["snapshot"].taken_at
        time.sleep(0.01)
        self.assertGreater(got["refresh"]().taken_at, before)
        got["save_ui"](got["ui"])
        self.assertFalse(os.path.exists(state.ui_path()))   # demo never persists UI state

    def test_bench_hook(self):
        marker = os.path.join(self.tmp.name, "ff")
        with mock.patch.dict(os.environ, {"AGENTREE_BENCH_FIRST_FRAME": marker}):
            hook = cli._bench_hook()
        with self.assertRaises(SystemExit):
            hook()
        with open(marker) as fh:
            t, ms = (float(x) for x in fh.read().split())
        self.assertAlmostEqual(t, time.time(), delta=5)
        self.assertGreater(ms, 0)

    def test_main_kills_children_on_exit(self):
        from agentree.actions import exec as ex
        try:
            with mock.patch.object(cli, "run", return_value=0), mock.patch.object(ex, "shutdown") as sd, \
                    mock.patch("atexit.register") as reg:
                self.assertEqual(cli.main(["--json"]), 0)
            sd.assert_called_once_with()
            reg.assert_called_once_with(cli._kill_children)
        finally:
            ex._reopen()

    def test_import_is_cheap(self):
        code = ("import sys, agentree.cli, agentree.model.tree; "
                "print(sorted(m for m in sys.modules if m.startswith('agentree')))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
        mods = eval(r.stdout)
        self.assertNotIn("agentree.model.sample", mods)
        self.assertNotIn("agentree.model.link", mods)
        self.assertNotIn("agentree.ui.app", mods)

    def test_model_lazy_exports(self):
        import agentree.model as m
        self.assertTrue(callable(m.sample_snapshot))
        self.assertTrue(callable(m.render_plain))
        with self.assertRaises(AttributeError):
            m.nope  # noqa: B018


class ProviderCliTest(_Env):
    """Hidden provider commands are dispatched through the registry; core cli knows no provider."""

    def test_run_internal_dispatches_to_the_owner(self):
        try:
            from tests._fake_provider import FakeProvider, injected
        except ImportError:  # pragma: no cover
            from _fake_provider import FakeProvider, injected  # type: ignore
        fake = FakeProvider()
        with injected(fake):
            self.assertEqual(cli.run(["codex-hook", "--x", "1"]), 7)
            self.assertEqual(cli.run_internal(["codex-hook"]), 7)
            self.assertIsNone(cli.run_internal(["spawn"]))          # not a provider command
            with mock.patch.object(cli, "cmd_doctor", return_value=0) as doc:
                self.assertEqual(cli.run(["doctor"]), 0)            # ordinary commands fall through
            doc.assert_called_once_with()
        self.assertEqual(fake.calls, [("run_internal", "codex-hook", ("--x", "1")),
                                      ("run_internal", "codex-hook", ())])
        self.assertIsNone(cli.run_internal(["codex-hook"]))          # not registered outside the override

    def test_internal_command_path_is_tiny(self):
        # the claude hook (run on every Claude event) must not import gather / UI / actions
        code = ("import io, sys; sys.stdin = io.StringIO(''); import agentree.cli as c; rc = c.run(['claude-hook']); "
                "print(rc, sorted(m for m in sys.modules if m.startswith('agentree')))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=30,
                           env=dict(os.environ, AGENTREE_STATE_DIR=self.state_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        rc, mods = r.stdout.split(" ", 1)
        mods = eval(mods)
        self.assertEqual(rc, "0")
        self.assertIn("agentree.providers.claude", mods)
        for m in ("agentree.gather", "agentree.ui.app", "agentree.model.tree", "agentree.actions.spawn", "agentree.actions.agent",
                  "agentree.doctor", "agentree.providers.opencode"):
            self.assertNotIn(m, mods)

    def test_spawn_agent_choices_are_the_registry_ids(self):
        from agentree.providers import registry
        ns = cli._parser().parse_args(["spawn", "proj", "b"])
        self.assertIsNone(ns.agent)
        self.assertEqual(cli.spawn_agent(ns, config.Config()), registry.default_id(config.Config()))
        for pid in registry.ids():
            self.assertEqual(cli._parser().parse_args(["spawn", "proj", "b", "--agent", pid]).agent, pid)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli._parser().parse_args(["spawn", "proj", "b", "--agent", "gpt"])


class BuildTest(_Env):
    def test_zipapp_json_cached(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            import build
        finally:
            sys.path.pop(0)
        out = os.path.join(self.tmp.name, "dist", "agentree")
        build.build(out, python=sys.executable)
        with open(out, "rb") as fh:
            self.assertEqual(fh.readline(), f"#!{sys.executable}\n".encode())
        import zipfile
        names = zipfile.ZipFile(out).namelist()
        self.assertIn("__main__.py", names)
        self.assertIn("agentree/cli.py", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.startswith(("tests/", "scripts/"))])
        snap = sample_snapshot(NOW)
        state.save_cache(snap)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([out, "--json", "--cached"], capture_output=True, text=True, env=env,
                           cwd=self.tmp.name, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(replace(from_dict(json.loads(r.stdout)), stale=False), snap)
        self.assertEqual(os.stat(out).st_mode & 0o777, 0o755)


def ccmod_spec():
    from agentree.providers import registry
    return registry.spec("claude")


def _ctx():
    from agentree.providers.base import FetchContext
    return FetchContext(timeout_s=0.1, now=None, pane_titles=lambda: {})


if __name__ == "__main__":
    unittest.main()
