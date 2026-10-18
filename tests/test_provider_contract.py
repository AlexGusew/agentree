"""Provider contract through the generic core: a fake third provider ("codex") goes through
gather(), Gatherer.agents() and LiveStatus with no core change (routing, PID precedence,
quick_failure policies, warnings source = provider id, watcher/refresh policy), and provider
modules stay side-effect free on import."""
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from typing import Literal
from unittest import mock

from agentree import config
from agentree import gather as gmod
from agentree.model.sample import _with_demo_agents, sample_raw, sample_snapshot
from agentree.providers import registry
from agentree.providers.base import DemoAgent, ProviderSpec, RefreshPolicy
from agentree.types import Snapshot, SourceWarning, ZPane

NOW = 1_800_000_000.0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODEX = ProviderSpec(id="codex", label="Codex", module="tests.no_such_module", command_key="codex_cmd",
                     default_command="codex")


class FakeWatcher:
    def __init__(self, poke):
        self.poke = poke
        self.updates = []
        self.closed = False

    def update(self, agents):
        self.updates.append(sorted((a.provider, a.pid) for a in agents))

    def healthy(self):
        return {}

    def close(self):
        self.closed = True


class FakeProvider:
    """Minimal provider: canned agents/warnings, records every call."""

    def __init__(self, pid_id="codex", agents=(), warnings=(), quick_failure="keep_provider",
                 policy=RefreshPolicy(7.0), demo=()):
        self.id = pid_id
        self.quick_failure: Literal["keep_snapshot", "keep_provider"] = quick_failure
        self.agents = tuple(agents)
        self.warnings = list(warnings)
        self.policy = policy
        self.demo = tuple(demo)
        self.full_calls = []
        self.quick_calls = []
        self.policy_calls = []
        self.watchers = []
        self.fail_quick: BaseException | None = None     # raised from fetch_quick
        self.fail_watcher = False

    # -- the rest of the Provider protocol (unused by the generic gather / live core)
    def matches_argv(self, argv):
        return False

    def find_pids(self, rows):
        return ()

    def session_id_from_argv(self, argv):
        return None

    def resolve_command(self, cfg):
        return None

    def build_launch(self, cfg, request):
        raise NotImplementedError

    def execute_setup(self, step, *, runner, state_dir):
        raise ValueError(step.operation)

    def find_launched_pids(self, session, pane_ids, *, session_id, timeout_s):
        return ()

    def doctor_jobs(self, cfg, probe):
        return ()

    def run_internal(self, command, argv):
        raise KeyError(command)

    def fetch_budget_s(self, timeout_s, *, quick):
        return timeout_s

    def fetch_full(self, ctx):
        self.full_calls.append(ctx)
        return self.agents, list(self.warnings)

    def fetch_quick(self, agents, ctx):
        self.quick_calls.append(sorted((a.provider, a.pid) for a in agents))
        if self.fail_quick is not None:
            raise self.fail_quick
        got = {a.pid for a in agents}
        return (tuple(replace(a, status="busy") for a in self.agents if a.pid in got),
                [SourceWarning(self.id, "quick")])

    def start_watcher(self, poke):
        if self.fail_watcher:
            raise RuntimeError("no feed")
        w = FakeWatcher(poke)
        self.watchers.append(w)
        return w

    def refresh_policy(self, agents, watcher):
        self.policy_calls.append((sorted(a.pid for a in agents), watcher))
        return self.policy

    def demo_agents(self, ctx):
        return self.demo


class _Env(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": os.path.join(tmp.name, "st"),
                                           "AGENTREE_CONFIG": os.path.join(tmp.name, "absent.toml")})
        env.start()
        self.addCleanup(env.stop)
        gmod._inflight.clear()
        self.addCleanup(gmod._inflight.clear)
        self.raw = sample_raw(NOW)
        self.cfg = config.Config(source_timeout_s=0.3, git_budget_s=0.3)
        for target, value in (("agentree.sources.zellij.fetch", (self.raw["sessions"], [])),
                              ("agentree.sources.git.fetch", (self.raw["repos"], [])),
                              ("agentree.sources.kitty.fetch", ((), [])),
                              ("agentree.sources.zellij.fetch_pane_titles", ({}, []))):
            p = mock.patch(target, lambda *a, _v=value, **kw: _v)
            p.start()
            self.addCleanup(p.stop)
        # an opencode agent of the sample, re-labelled as codex (placed like any agent)
        oc = next(a for a in self.raw["agents"] if a.provider == "opencode" and a.session_name)
        self.oc_agent = oc
        self.cx_agent = replace(oc, pid=90001, provider="codex", status="idle", meta={"model": "x"})


class GatherContractTest(_Env):
    def test_registry_registered_fake_goes_through_gather(self):
        fake = FakeProvider(agents=(self.cx_agent,), warnings=[SourceWarning("codex", "w")])
        with mock.patch("agentree.providers.opencode.fetch", lambda *a, **kw: ((self.oc_agent,), [])), \
                mock.patch("agentree.providers.claude.fetch", lambda *a, **kw: ((), [])), \
                registry.override(specs=(*registry.specs(), CODEX), providers={"codex": fake}):
            snap = gmod.gather(self.cfg, now=NOW)
        got = {a.pid: a for a in gmod.snapshot_agents(snap)}
        self.assertEqual(got[self.cx_agent.pid].provider, "codex")
        self.assertEqual(got[self.cx_agent.pid].meta, {"model": "x"})
        self.assertIn(self.oc_agent.pid, got)
        self.assertIn(("codex", "w"), [(w.source, w.message) for w in snap.warnings])
        self.assertIn("codex", gmod.TIMINGS)
        # placed under its worktree, same as the opencode agent it was cloned from
        placed = {a.pid: (n.session.name, w.worktree.path) for n in snap.sessions for w in n.worktrees
                  for a in w.agents}
        self.assertEqual(placed[self.cx_agent.pid], placed[self.oc_agent.pid])
        # one full fetch with a FetchContext carrying the zellij pane titles
        (ctx,) = fake.full_calls
        self.assertEqual(ctx.timeout_s, self.cfg.source_timeout_s)
        self.assertEqual(ctx.pane_titles(), gmod.pane_titles(self.raw["sessions"]))

    def test_pid_precedence_follows_provider_order(self):
        dup = replace(self.cx_agent, pid=self.oc_agent.pid)
        a = FakeProvider("first", agents=(replace(dup, provider="first"),))
        b = FakeProvider("second", agents=(replace(dup, provider="second"), self.cx_agent))
        for order, want in (([a, b], "first"), ([b, a], "second")):
            with self.subTest(order=[p.id for p in order]):
                gmod._inflight.clear()
                snap = gmod.gather(self.cfg, now=NOW, providers=order)
                got = {x.pid: x.provider for x in gmod.snapshot_agents(snap)}
                self.assertEqual(got[dup.pid], want)
                self.assertEqual(len(got), 2)

    def test_failure_and_hang_are_warnings_under_provider_id(self):
        boom = FakeProvider("codex")
        boom.fetch_full = mock.Mock(side_effect=RuntimeError("down"))
        hang_release = threading.Event()
        self.addCleanup(hang_release.set)
        hang = FakeProvider("slow")
        hang.fetch_full = lambda ctx: (hang_release.wait(5), ((), []))[1]
        hang.fetch_budget_s = lambda timeout_s, *, quick: 0.05
        ok = FakeProvider("fine", agents=(self.cx_agent,))
        snap = gmod.gather(self.cfg, now=NOW, providers=[boom, hang, ok])
        msgs = {w.source: w.message for w in snap.warnings}
        self.assertIn("down", msgs["codex"])
        self.assertIn("timed out", msgs["slow"])
        self.assertEqual([x.pid for x in gmod.snapshot_agents(snap)], [self.cx_agent.pid])
        # the hung provider is not launched again while it is still running
        snap = gmod.gather(self.cfg, now=NOW, providers=[hang])
        self.assertEqual({w.source: w.message for w in snap.warnings}["slow"], "previous refresh still running")


class GathererContractTest(_Env):
    def gatherer(self, *providers):
        g = gmod.Gatherer(providers)
        prev = g.full(self.cfg, now=NOW)
        return g, prev

    def test_quick_routes_only_own_agents(self):
        other = FakeProvider("other", agents=(replace(self.oc_agent, provider="other"),),
                             warnings=[SourceWarning("other", "kept")])
        cx = FakeProvider(agents=(self.cx_agent,), warnings=[SourceWarning("codex", "full")])
        g, prev = self.gatherer(cx, other)
        other.agents = ()                       # "other" has agents in prev; codex returns busy
        snap = g.agents(self.cfg, prev, now=NOW + 3)
        self.assertEqual(cx.quick_calls, [[("codex", self.cx_agent.pid)]])
        self.assertEqual(other.quick_calls, [[("other", self.oc_agent.pid)]])
        got = {a.pid: a for a in gmod.snapshot_agents(snap)}
        self.assertEqual(got[self.cx_agent.pid].status, "busy")
        self.assertNotIn(self.oc_agent.pid, got)          # its own quick fetch dropped it
        self.assertEqual([(w.source, w.message) for w in snap.warnings if w.source in ("codex", "other")],
                         [("codex", "quick"), ("other", "quick")])
        self.assertEqual(snap.taken_at, NOW + 3)
        self.assertIn("codex", g.timings)

    def test_provider_without_agents_is_skipped_and_keeps_warnings(self):
        cx = FakeProvider(agents=(self.cx_agent,))
        idle = FakeProvider("idle", warnings=[SourceWarning("idle", "not installed")])
        g, prev = self.gatherer(cx, idle)
        snap = g.agents(self.cfg, prev, now=NOW)
        self.assertEqual(idle.quick_calls, [])
        self.assertIn(("idle", "not installed"), [(w.source, w.message) for w in snap.warnings])

    def test_quick_failure_policies(self):
        for policy in ("keep_snapshot", "keep_provider"):
            with self.subTest(policy=policy):
                gmod._inflight.clear()
                cx = FakeProvider(agents=(self.cx_agent,), warnings=[SourceWarning("codex", "full")],
                                  quick_failure=policy)
                other = FakeProvider("other", agents=(replace(self.oc_agent, provider="other"),))
                g, prev = self.gatherer(cx, other)
                cx.fail_quick = RuntimeError("quick down")
                snap = g.agents(self.cfg, prev, now=NOW + 1)
                if policy == "keep_snapshot":
                    self.assertIs(snap, prev)
                    continue
                got = {a.pid: a for a in gmod.snapshot_agents(snap)}
                self.assertEqual(got[self.cx_agent.pid], self.cx_agent)          # previous record kept
                self.assertEqual(got[self.oc_agent.pid].status, "busy")          # the other refreshed
                self.assertEqual([(w.source, w.message) for w in snap.warnings if w.source == "codex"],
                                 [("codex", "full")])                            # previous warnings kept
                self.assertNotIn("quick down", " ".join(w.message for w in snap.warnings))

    def test_default_gatherer_follows_registry_override(self):
        fake = FakeProvider(agents=(self.cx_agent,))
        with mock.patch("agentree.providers.opencode.fetch", lambda *a, **kw: ((), [])), \
                mock.patch("agentree.providers.claude.fetch", lambda *a, **kw: ((), [])), \
                registry.override(specs=(*registry.specs(), CODEX), providers={"codex": fake}):
            prev = gmod.gather(self.cfg, now=NOW)
            snap = gmod.gather_agents(self.cfg, prev, now=NOW)
        self.assertEqual(fake.quick_calls, [[("codex", self.cx_agent.pid)]])
        self.assertEqual([a.status for a in gmod.snapshot_agents(snap)], ["busy"])


class LiveStatusContractTest(unittest.TestCase):
    def setUp(self):
        base = sample_snapshot(NOW)
        oc = next(a for a in gmod.snapshot_agents(base) if a.provider == "opencode")
        self.cx1 = replace(oc, pid=91001, provider="codex")
        self.cx2 = replace(oc, pid=91002, provider="codex")
        self.zz = replace(oc, pid=92001, provider="zz")
        self.snap = Snapshot(taken_at=NOW, orphans=base.orphans.__class__(agents=(self.cx1, self.zz, self.cx2)))

    def live(self, *providers):
        live = gmod.LiveStatus(config.Config(), full=lambda cfg: self.snap, quick=lambda cfg, prev: prev,
                               providers=providers)
        self.addCleanup(live.close)
        return live

    def test_watchers_get_own_agents_and_policies_combine(self):
        cx = FakeProvider(policy=RefreshPolicy(9.0, 4.0))
        zz = FakeProvider("zz", policy=RefreshPolicy(6.0))
        none = FakeProvider("none", policy=RefreshPolicy(0.5, 0.1))      # no agents: no say
        live = self.live(cx, zz, none)
        self.assertEqual(live.interval(), live.POLL_EVERY)               # not started yet
        live.refresh()                                                   # before start(): remembered
        live.start(lambda: None)
        self.assertEqual(set(live.watchers), {"codex", "zz", "none"})
        self.assertEqual(cx.watchers[0].updates, [[("codex", 91001), ("codex", 91002)]])
        self.assertEqual(zz.watchers[0].updates, [[("zz", 92001)]])
        self.assertEqual(none.watchers[0].updates, [[]])
        self.assertEqual((live.interval(), live.quick_interval()), (6.0, 4.0))
        self.assertEqual(cx.policy_calls[-1], ([91001, 91002], cx.watchers[0]))
        self.assertEqual(none.policy_calls, [])
        live.quick(self.snap)
        self.assertEqual(len(cx.watchers[0].updates), 2)
        live.close()
        self.assertTrue(all(w.closed for p in (cx, zz, none) for w in p.watchers))
        self.assertEqual(live.watchers, {})

    def test_failing_watcher_falls_back_to_polling(self):
        cx = FakeProvider(policy=RefreshPolicy(9.0))
        cx.fail_watcher = True
        live = self.live(cx)
        live.start(lambda: None)
        live.refresh()
        self.assertEqual(live.watchers, {"codex": None})
        self.assertEqual(live.interval(), 9.0)
        self.assertEqual(cx.policy_calls[-1], ([91001, 91002], None))
        self.assertIsNone(live.quick_interval())

    def test_start_after_close_starts_nothing_lasting(self):
        cx = FakeProvider()
        live = self.live(cx)
        live.close()
        live.start(lambda: None)
        self.assertTrue(cx.watchers[0].closed)
        self.assertEqual(live.watchers, {})


class DemoContractTest(unittest.TestCase):
    def test_fake_provider_demo_agents_are_inserted(self):
        raw = sample_raw(NOW)
        s = next(x for x in raw["sessions"] if x.tabs)
        t = s.tabs[0]
        pane = ZPane(pane_id=777, is_plugin=False, tab_id=t.tab_id, title="codex", command="codex",
                     argv=("codex",), cwd="/w")
        agent = replace(raw["agents"][0], pid=1, provider="codex", session_name=s.name, pane_id=777)
        fake = FakeProvider(demo=(DemoAgent(agent, s.name, t.tab_id, pane),))
        sessions, agents = _with_demo_agents(raw["sessions"], NOW, {}, providers=[fake])
        self.assertEqual([a.provider for a in agents], ["codex"])
        got = next(x for x in sessions if x.name == s.name).tabs[0]
        self.assertIn(777, [p.pane_id for p in got.panes])
        ids = [(p.is_plugin, p.pane_id) for p in got.panes]
        self.assertEqual(ids, sorted(ids))


class ImportSideEffectsTest(unittest.TestCase):
    def run_py(self, code):
        env = {**os.environ, "PYTHONPATH": ROOT}
        with tempfile.TemporaryDirectory() as tmp:
            env.update(AGENTREE_STATE_DIR=os.path.join(tmp, "st"), CLAUDE_CONFIG_DIR=os.path.join(tmp, "cc"),
                       HOME=os.path.join(tmp, "home"))
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                               cwd=tmp, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(sorted(os.listdir(tmp)), [])                   # nothing written
        return r.stdout.split()

    def test_registry_and_gather_import_no_provider_module(self):
        out = self.run_py(
            "import sys\n"
            "from agentree.providers import registry\n"
            "import agentree.gather, agentree.model.snapshot\n"
            "registry.specs(); registry.ids(); registry.internal_owner('claude-hook')\n"
            "print(sorted(m for m in sys.modules if m.startswith('agentree.providers.') and m not in\n"
            "      ('agentree.providers.base', 'agentree.providers.registry')))\n")
        self.assertEqual(out, ["[]"])

    def test_provider_import_and_instantiation_have_no_side_effects(self):
        out = self.run_py(
            "import subprocess, os, threading\n"
            "def no(*a, **kw): raise AssertionError('process started')\n"
            "subprocess.Popen = no; subprocess.run = no; os.system = no\n"
            "import shutil\n"
            "shutil.which = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('executable lookup'))\n"
            "from agentree.providers import registry\n"
            "ps = registry.all_providers()\n"
            "print(','.join(p.id for p in ps), threading.active_count())\n")
        self.assertEqual(out, ["opencode,claude", "1"])


if __name__ == "__main__":
    unittest.main()
