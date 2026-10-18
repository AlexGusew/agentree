import json
import os
import tempfile
import tomllib
import unittest
from dataclasses import replace
from unittest import mock

from agentree import config, paths, state
from agentree.model.sample import sample_snapshot
from agentree.types import UiState

NOW = 1_800_000_000.0


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.toml")
        self.env = mock.patch.dict(os.environ, {"AGENTREE_CONFIG": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, text):
        with open(self.path, "w") as fh:
            fh.write(text)

    def test_missing_file_defaults(self):
        c = config.load_config()
        self.assertEqual(c.repo_roots, ("~/Projects/*",))
        self.assertEqual(c.worktree_template, "{repo}/.worktrees/{slug}")
        self.assertEqual(c.branch_prefix, "agent/")
        self.assertEqual(c.accent, "#e5b454")
        self.assertEqual(c.layouts, ("agent", "default"))
        self.assertEqual(c.kitty_socket_glob, "/tmp/kitty.sock-*")
        self.assertEqual(c.zellij_socket_dirs, ())
        self.assertEqual(c.install_commands["bun.lockb"], "bun install")
        self.assertEqual(c.source_timeout_s, 1.0)
        self.assertEqual(c.git_budget_s, 2.5)
        self.assertEqual(c.opencode_cmd, "opencode")
        self.assertEqual(c.warnings, ())
        self.assertEqual(c.default_ui(), UiState())

    def test_values_and_unknown_keys(self):
        self.write('''
accent = "#ff0000"
repo_roots = ["~/code/*", "/src/x"]
bogus = 1
source_timeout_s = "fast"
[default_filters]
worktree_mode = "dirty"
panes = true
nope = 1
[install_commands]
"Cargo.lock" = false
"uv.lock" = "uv sync"
''')
        c = config.load_config()
        self.assertEqual(c.accent, "#ff0000")
        self.assertEqual(c.repo_roots, ("~/code/*", "/src/x"))
        self.assertEqual(c.source_timeout_s, 1.0)
        self.assertNotIn("Cargo.lock", c.install_commands)
        self.assertEqual(c.install_commands["uv.lock"], "uv sync")
        self.assertEqual(c.default_ui(), UiState(worktree_mode="dirty", show_panes=True))
        self.assertEqual(len(c.warnings), 3)
        self.assertTrue(any("bogus" in w for w in c.warnings))
        self.assertTrue(any("default_filters.nope" in w for w in c.warnings))
        self.assertEqual(c.path, self.path)

    def test_claude_keys(self):
        c = config.Config()
        self.assertEqual((c.claude_cmd, c.default_agent), ("claude", "opencode"))
        c = config.from_mapping({"claude_cmd": "~/.local/bin/claude --model opus", "default_agent": "claude"})
        self.assertEqual((c.claude_cmd, c.default_agent, c.warnings), ("~/.local/bin/claude --model opus",
                                                                        "claude", ()))
        c = config.from_mapping({"claude_cmd": 3, "default_agent": "gpt"})
        self.assertEqual((c.claude_cmd, c.default_agent), ("claude", "opencode"))
        self.assertIn("claude_cmd: expected a string", c.warnings)
        self.assertIn("default_agent: expected one of opencode, claude", c.warnings)
        c = config.from_mapping({"default_agent": True})
        self.assertEqual(c.default_agent, "opencode")
        self.assertIn("default_agent: expected a string", c.warnings)

    def test_provider_commands(self):
        c = config.Config()
        self.assertEqual(c.provider_commands, {"opencode": "opencode", "claude": "claude"})
        self.assertEqual((c.provider_command("opencode"), c.provider_command("claude")),
                         ("opencode", "claude"))
        with self.assertRaises(KeyError):
            c.provider_command("nope")
        c = config.from_mapping({"opencode_cmd": "/x/oc --auto", "claude_cmd": "cl"})
        self.assertEqual((c.provider_command("opencode"), c.opencode_cmd, c.provider_command("claude"), c.claude_cmd),
                         ("/x/oc --auto", "/x/oc --auto", "cl", "cl"))
        c = config.from_mapping({"opencode_cmd": ""})             # blank kept verbatim, as before
        self.assertEqual((c.opencode_cmd, c.provider_command("opencode"), c.warnings), ("", "", ()))
        c = config.from_mapping({"opencode_cmd": ["x"]})
        self.assertEqual(c.provider_command("opencode"), "opencode")
        self.assertEqual(c.warnings, ("opencode_cmd: expected a string",))
        # legacy fields and provider_commands stay in sync; given legacy fields win
        c = config.Config(opencode_cmd="/a", provider_commands={"claude": "/c", "opencode": "/ignored"})
        self.assertEqual((c.opencode_cmd, c.claude_cmd, c.provider_commands),
                         ("/a", "/c", {"opencode": "/a", "claude": "/c"}))
        r = replace(c, claude_cmd="/d")
        self.assertEqual((r.claude_cmd, r.provider_command("claude"), r.provider_command("opencode")),
                         ("/d", "/d", "/a"))
        r = replace(c, provider_commands={**c.provider_commands, "x": "/x"})
        self.assertEqual(r.provider_command("x"), "/x")
        self.assertNotIn("provider_commands", tomllib.loads(config.example_toml()))
        c = config.from_mapping({"provider_commands": {"opencode": "/x"}})
        self.assertEqual(c.warnings, ("unknown config key 'provider_commands'",))
        self.assertEqual(c.opencode_cmd, "opencode")

    def test_fake_provider_via_registry_override(self):
        from agentree.providers import registry
        from agentree.providers.base import ProviderSpec

        codex = ProviderSpec(id="codex", label="Codex", module="tests.no_such_module", command_key="codex_cmd",
                             default_command="codex --full-auto")
        c = config.from_mapping({"codex_cmd": "codex -q", "default_agent": "codex"})
        self.assertEqual(c.default_agent, "opencode")             # not registered: rejected
        self.assertIn("unknown config key 'codex_cmd'", c.warnings)
        self.assertIn("default_agent: expected one of opencode, claude", c.warnings)
        with registry.override(specs=(*registry.specs(), codex)):
            c = config.from_mapping({"codex_cmd": "codex -q", "default_agent": "codex"})
            self.assertEqual((c.warnings, c.default_agent, c.provider_command("codex")), ((), "codex", "codex -q"))
            self.assertEqual(registry.default_id(c), "codex")
            self.assertEqual(config.Config().provider_command("codex"), "codex --full-auto")   # descriptor default
            c = config.from_mapping({"codex_cmd": 1, "default_agent": "gpt"})
            self.assertEqual(c.warnings, ("codex_cmd: expected a string",
                                          "default_agent: expected one of opencode, claude, codex"))
            self.assertEqual((c.provider_command("codex"), c.default_agent), ("codex --full-auto", "opencode"))
            text = config.example_toml()
            self.assertIn('# Command used to start a Codex agent.\ncodex_cmd = "codex --full-auto"\n', text)
            self.assertIn("without --agent: opencode | claude | codex.", text)
            c = config.from_mapping(tomllib.loads(text))
            self.assertEqual((c.warnings, c.provider_command("codex")), ((), "codex --full-auto"))
            self.write('codex_cmd = "/opt/codex"\ndefault_agent = "codex"\n')
            c = config.load_config()
            self.assertEqual((c.warnings, c.provider_command("codex"), c.default_agent), ((), "/opt/codex", "codex"))
        self.assertNotIn("codex_cmd", config.example_toml())
        with registry.override(specs=(codex,)):                   # without claude, claude_cmd is unknown
            c = config.from_mapping({"claude_cmd": "x"})
            self.assertEqual(c.warnings, ("unknown config key 'claude_cmd'",))
            self.assertEqual(config.Config().claude_cmd, "claude")

    def test_git_budget(self):
        self.write("git_budget_s = 4\n")
        self.assertEqual(config.load_config().git_budget_s, 4.0)
        self.write("git_budget_s = -1\n")
        c = config.load_config()
        self.assertEqual(c.git_budget_s, 2.5)
        self.assertTrue(any("git_budget_s" in w for w in c.warnings))

    def test_non_finite_numbers_rejected(self):
        for raw in ("nan", "inf", "-inf", "+inf"):
            with self.subTest(raw):
                self.write(f"source_timeout_s = {raw}\ngit_budget_s = {raw}\n")
                c = config.load_config()
                self.assertEqual((c.source_timeout_s, c.git_budget_s), (1.0, 2.5))
                self.assertEqual(len(c.warnings), 2)
                self.assertTrue(all("finite" in w for w in c.warnings))

    def test_parse_error_is_warning(self):
        self.write("this is = = not toml")
        c = config.load_config()
        self.assertEqual(c.accent, "#e5b454")
        self.assertEqual(len(c.warnings), 1)

    def test_example_round_trips(self):
        data = tomllib.loads(config.example_toml())
        c = config.from_mapping(data)
        self.assertEqual(c.warnings, ())
        d = config.Config()
        self.assertEqual((c.repo_roots, c.layouts, c.install_commands, c.default_filters),
                         (d.repo_roots, d.layouts, d.install_commands, d.default_filters))
        self.assertEqual((c.provider_commands, c.default_agent), (d.provider_commands, d.default_agent))
        self.assertEqual({k for k in data if k.endswith("_cmd")}, {"opencode_cmd", "claude_cmd"})

    def test_worktree_path(self):
        c = config.Config()
        self.assertEqual(c.worktree_path("/p/repo/", "x"), "/p/repo/.worktrees/x")


class PathsTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(paths.config_path({}, "/h"), "/h/.config/agentree/config.toml")
        self.assertEqual(paths.state_dir({}, "/h"), "/h/.local/state/agentree")

    def test_xdg(self):
        env = {"XDG_CONFIG_HOME": "/x/cfg", "XDG_STATE_HOME": "/x/st"}
        self.assertEqual(paths.config_path(env, "/h"), "/x/cfg/agentree/config.toml")
        self.assertEqual(paths.state_dir(env, "/h"), "/x/st/agentree")
        rel = {"XDG_CONFIG_HOME": "cfg", "XDG_STATE_HOME": ""}          # relative / empty: ignored
        self.assertEqual(paths.config_path(rel, "/h"), "/h/.config/agentree/config.toml")
        self.assertEqual(paths.state_dir(rel, "/h"), "/h/.local/state/agentree")

    def test_overrides_win(self):
        env = {"XDG_CONFIG_HOME": "/x", "XDG_STATE_HOME": "/x",
               "AGENTREE_CONFIG": "~/my.toml", "AGENTREE_STATE_DIR": "~/st"}
        self.assertEqual(paths.config_path(env, "/h"), "/h/my.toml")
        self.assertEqual(paths.state_dir(env, "/h"), "/h/st")
        with mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": "~/st"}):
            from agentree.actions import exec as ex
            self.assertEqual(ex.state_dir(), os.path.expanduser("~/st"))
            self.assertEqual(state.state_dir(), os.path.expanduser("~/st"))


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "state")
        self.env = mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": self.dir})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_paths(self):
        self.assertEqual(state.state_dir(), self.dir)
        self.assertEqual(state.cache_path(), os.path.join(self.dir, "snapshot.json"))

    def test_ui_round_trip(self):
        d = UiState(worktree_mode="dirty")
        self.assertEqual(state.load_ui(d), d)
        ui = UiState(worktree_mode="hide", show_agents=False, show_panes=True, show_detached=False,
                     text="sso", folds=(("s:a", True), ("w:a:/x", False)), selected="s:a")
        state.save_ui(ui)
        self.assertEqual(state.load_ui(d), ui)
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith(".tmp")], [])

    def test_ui_corrupt(self):
        os.makedirs(self.dir)
        with open(state.ui_path(), "w") as fh:
            fh.write("{nope")
        self.assertEqual(state.load_ui(UiState(show_panes=True)), UiState(show_panes=True))
        with open(state.ui_path(), "w") as fh:
            json.dump({"worktree_mode": "weird", "show_agents": "yes", "folds": [["k", True], [1, 2], "x"],
                       "text": 5}, fh)
        self.assertEqual(state.load_ui(), UiState(folds=(("k", True),)))
        # each bad field falls back to its default; good fields survive
        base = UiState(worktree_mode="dirty", show_panes=True, text="q", selected="s:x", folds=(("a", True),))
        with open(state.ui_path(), "w") as fh:
            json.dump({"worktree_mode": 3, "show_agents": False, "show_panes": 0, "show_detached": None,
                       "text": ["x"], "folds": {"k": True}, "selected": 7}, fh)
        self.assertEqual(state.load_ui(base), replace(base, show_agents=False))
        with open(state.ui_path(), "w") as fh:
            json.dump({"selected": None, "folds": [["k", "yes"], ["k2", False, 1], ["k3", False]]}, fh)
        self.assertEqual(state.load_ui(base), replace(base, selected=None, folds=(("k3", False),)))
        with open(state.ui_path(), "w") as fh:
            json.dump([1, 2], fh)
        self.assertEqual(state.load_ui(base), base)

    def test_links(self):
        self.assertEqual(state.load_links(), [])
        state.upsert_link({"worktreePath": "/p/a/", "session": "s", "tabName": "a", "agentPids": [1]})
        state.upsert_link({"worktreePath": "/p/b", "session": "s", "tabName": "b"})
        state.upsert_link({"worktreePath": "/p/a", "session": "t", "tabName": "a2", "agentPids": [2]})
        recs = state.load_links()
        self.assertEqual([(r["worktreePath"], r["session"]) for r in recs], [("/p/b", "s"), ("/p/a", "t")])
        self.assertEqual(recs[0]["agentPids"], [])
        self.assertTrue(state.remove_link("/p/a/"))
        self.assertFalse(state.remove_link("/p/zzz"))
        self.assertEqual([r["worktreePath"] for r in state.load_links()], ["/p/b"])
        with self.assertRaises(ValueError):
            state.upsert_link({"session": "s"})

    # ------------------------------------------------------------------ merge_link (M4 `o`)
    def test_merge_link_read_modify_write(self):
        state.upsert_link({"worktreePath": "/p/a", "session": "s", "tabName": "a", "note": "keep"})
        state.upsert_link({"worktreePath": "/p/b", "session": "s", "tabName": "b"})
        got = state.merge_link("/p/a/", lambda cur: {**cur, "agentPanes": [3]})
        self.assertEqual(got, {"worktreePath": "/p/a", "session": "s", "tabName": "a", "note": "keep",
                               "agentPids": [], "agentPanes": [3]})
        self.assertEqual([r["worktreePath"] for r in state.load_links()], ["/p/a", "/p/b"])   # order kept
        seen = []
        state.merge_link("/p/new", lambda cur: seen.append(cur) or {"session": "t"})
        self.assertEqual(seen, [None])
        self.assertEqual(state.load_links()[-1], {"worktreePath": "/p/new", "session": "t", "agentPids": []})
        self.assertIsNone(state.merge_link("/p/b", lambda cur: None))
        self.assertEqual([r["worktreePath"] for r in state.load_links()], ["/p/a", "/p/new"])
        with self.assertRaises(ValueError):
            state.merge_link("", lambda cur: cur)

    def _opened(self, pane, session="S"):
        from agentree.actions.agent import Opened
        return Opened(session, "/tmp/zd", 1, "x", pane, "/p/wt", False)

    @staticmethod
    def _nav():
        from types import SimpleNamespace
        return SimpleNamespace(runner=SimpleNamespace(internal=lambda _d, fn: fn()))

    def _slow_reads(self, delay=0.05):
        """Widen the read-modify-write window so unlocked writers would lose updates."""
        import time
        orig = state._load_links_unlocked

        def slow():
            recs = orig()
            time.sleep(delay)
            return recs
        return mock.patch.object(state, "_load_links_unlocked", slow)

    def test_concurrent_agent_panes_are_both_kept(self):
        import threading
        from agentree.actions.agent import record_link
        state.upsert_link({"worktreePath": "/p/wt", "session": "S", "tabName": "x", "tabId": 1,
                           "agentPids": [5], "note": "keep"})
        go = threading.Barrier(2)
        notes = []

        def add(pane, pid):
            go.wait()
            notes.append(record_link(self._nav(), self._opened(pane), [pid], state))
        with self._slow_reads():
            ts = [threading.Thread(target=add, args=(301, 1)), threading.Thread(target=add, args=(302, 2))]
            for t in ts:
                t.start()
            for t in ts:
                t.join(10)
        self.assertEqual(notes, [None, None])
        rec = state.load_links()[0]
        self.assertEqual((rec["agentPanes"], rec["agentPids"], rec["note"], rec["session"]),
                         ([301, 302], [1, 2, 5], "keep", "S"))

    def test_concurrent_rename_is_not_reverted(self):
        """A rename racing with `o`'s merge: whichever takes links.lock first, the record ends up
        under the new name — the merge never writes the old session back."""
        import threading
        import time
        from agentree.actions.agent import record_link
        for first in ("add", "rename"):
            with self.subTest(first=first):
                state.upsert_link({"worktreePath": "/p/wt", "session": "S", "tabName": "x", "tabId": 1,
                                   "agentPids": [], "note": "keep"})
                inside = threading.Event()
                orig = state._load_links_unlocked

                def slow():
                    recs = orig()
                    inside.set()                        # the first one holds links.lock now
                    time.sleep(0.1)
                    return recs
                notes = []

                def add():
                    notes.append(record_link(self._nav(), self._opened(301), [7], state))

                def rename():
                    state.rename_link_session("S", "S2")
                a, b = (add, rename) if first == "add" else (rename, add)
                with mock.patch.object(state, "_load_links_unlocked", slow):
                    t1 = threading.Thread(target=a)
                    t1.start()
                    self.assertTrue(inside.wait(5))
                    t2 = threading.Thread(target=b)      # starts while the first holds the lock
                    t2.start()
                    for t in (t1, t2):
                        t.join(10)
                rec = state.load_links()[0]
                self.assertEqual((rec["session"], rec["note"]), ("S2", "keep"))
                if first == "add":                      # merged as S, then renamed with it
                    self.assertEqual((rec["agentPanes"], rec["agentPids"], notes), ([301], [7], [None]))
                else:                                   # S2 is another session to this merge: untouched
                    self.assertNotIn("agentPanes", rec)
                    self.assertEqual(notes, ["link kept for S2; agent not recorded"])

    def test_record_for_another_session_is_untouched(self):
        from agentree.actions.agent import RenameEvidence, record_link
        before = {"worktreePath": "/p/wt", "session": "S2", "tabName": "y", "tabId": 4,
                  "agentPids": [1], "agentPanes": [2]}
        state.upsert_link(before)
        self.assertEqual(record_link(self._nav(), self._opened(301, "S"), [9], state),
                         "link kept for S2; agent not recorded")
        self.assertEqual(state.load_links(), [before])
        # a rename the caller verified (S2 -> S) takes it over and keeps the rest
        self.assertIsNone(record_link(self._nav(), self._opened(301, "S"), [9], state,
                                      evidence=RenameEvidence("S2", None, None), started=123.0))
        rec = state.load_links()[0]
        self.assertEqual((rec["session"], rec["agentPids"], rec["agentPanes"], rec["sessionStarted"]),
                         ("S", [1, 9], [2, 301], 123.0))

    def test_links_corrupt(self):
        os.makedirs(self.dir)
        with open(state.links_path(), "w") as fh:
            fh.write("[{]")
        self.assertEqual(state.load_links(), [])
        with open(state.links_path(), "w") as fh:
            json.dump([{"worktreePath": "/x"}, {"bad": 1}, 3], fh)
        self.assertEqual(state.load_links(), [{"worktreePath": "/x"}])

    def test_cache(self):
        self.assertIsNone(state.load_cache())
        snap = sample_snapshot(NOW)
        state.save_cache(snap)
        back = state.load_cache()
        self.assertTrue(back.stale)
        self.assertEqual(back.sessions, snap.sessions)
        self.assertEqual(back.orphans, snap.orphans)
        with open(state.cache_path(), "w") as fh:
            fh.write('{"taken_at": "x"')
        self.assertIsNone(state.load_cache())
        with open(state.cache_path(), "w") as fh:
            json.dump({"version": 1, "sessions": []}, fh)
        self.assertIsNone(state.load_cache())

    def test_cache_valid_json_wrong_types_is_miss(self):
        snap = sample_snapshot(NOW)
        state.save_cache(snap)
        with open(state.cache_path()) as fh:
            good = json.load(fh)

        def put(mutate):
            d = json.loads(json.dumps(good))
            mutate(d)
            with open(state.cache_path(), "w") as fh:
                json.dump(d, fh)
            return state.load_cache()

        s0 = lambda d: d["sessions"][0]                     # noqa: E731
        bad = [
            lambda d: s0(d)["session"].__setitem__("name", 123),
            lambda d: s0(d)["worktrees"][1]["agents"][0].__setitem__("status", "weird"),
            lambda d: s0(d)["session"].__setitem__("tabs", {"a": 1}),
            lambda d: s0(d)["worktrees"][0]["worktree"].__delitem__("repo_root"),
            lambda d: s0(d)["session"]["tabs"][0].__setitem__("active", "false"),
            lambda d: d.__setitem__("sessions", [1, 2]),
            lambda d: d.__setitem__("orphans", "none"),
        ]
        for i, mutate in enumerate(bad):
            with self.subTest(i):
                self.assertIsNone(put(mutate))
        for text in ("[]", "null", "1", '"x"', "[" * 100_000 + "]" * 100_000):
            with self.subTest(text[:10]):
                with open(state.cache_path(), "w") as fh:
                    fh.write(text)
                self.assertIsNone(state.load_cache())
        # sanity: the untouched file still loads exactly
        back = put(lambda d: None)
        self.assertEqual(replace(back, stale=False), snap)


if __name__ == "__main__":
    unittest.main()
