"""Pure-logic tests of the live jump harness (scripts/live_jump.py): ownership and window selection.
Nothing here talks to zellij or kitty: the harness's shell runner is injected."""
import importlib.util
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("live_jump", os.path.join(ROOT, "scripts", "live_jump.py"))
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


class FakeSh:
    """Injected runner: a fake zellij with a set of existing session names."""

    def __init__(self, existing=(), create_rc=0, create_makes_session=True):
        self.existing = set(existing)
        self.create_rc = create_rc
        self.create_makes_session = create_makes_session
        self.calls = []

    def __call__(self, argv, *, env=None, cwd=None, check_rc=False):
        self.calls.append(list(argv))
        out, rc = "", 0
        if argv[1:3] == ["list-sessions", "-n"]:
            out = "\n".join(sorted(self.existing))
        elif argv[1:3] == ["attach", "--create-background"]:
            if self.create_makes_session:
                self.existing.add(argv[3])
            rc = self.create_rc
        elif argv[1] in ("kill-session", "delete-session"):
            self.existing.discard(argv[2])
        return subprocess.CompletedProcess(argv, rc, out, "")

    def targets(self, *cmds):
        return [c[2] for c in self.calls if c[1] in cmds]


class OwnershipTest(unittest.TestCase):
    def test_pre_existing_session_is_never_killed(self):
        sh = FakeSh(existing={live.A})                    # someone else's session with our name
        owned = live.Ownership()
        with self.assertRaises(live.Abort):
            live.create_session(live.A, "/x.kdl", owned, sh=sh)
        self.assertEqual(owned.sessions, [])
        live.cleanup_sessions(owned, sh=sh)               # what main()'s finally does
        self.assertEqual(sh.targets("kill-session", "delete-session"), [])
        self.assertNotIn("attach", [c[1] for c in sh.calls])
        with self.assertRaises(live.Abort):               # and it cannot be deleted explicitly either
            live.kill_session(live.A, owned, sh=sh)
        self.assertIn(live.A, sh.existing)

    def test_created_sessions_are_registered_and_cleaned(self):
        sh = FakeSh()
        owned = live.Ownership()
        live.create_session(live.A, "/a.kdl", owned, sh=sh)
        live.create_session(live.B, "/b.kdl", owned, sh=sh)
        self.assertEqual(owned.sessions, [live.A, live.B])
        live.cleanup_sessions(owned, sh=sh)
        self.assertEqual(sh.targets("kill-session"), [live.A, live.B])
        self.assertEqual(sh.existing, set())

    def test_partial_creation(self):
        # the create command failed but did bring the session up: ours, cleaned up
        sh = FakeSh(create_rc=1, create_makes_session=True)
        owned = live.Ownership()
        with self.assertRaises(RuntimeError):
            live.create_session(live.A, "/a.kdl", owned, sh=sh)
        self.assertEqual(owned.sessions, [live.A])
        # failed and nothing appeared: nothing registered, nothing to clean
        sh2 = FakeSh(create_rc=1, create_makes_session=False)
        owned2 = live.Ownership()
        with self.assertRaises(RuntimeError):
            live.create_session(live.B, "/b.kdl", owned2, sh=sh2)
        self.assertEqual(owned2.sessions, [])

    def test_only_this_runs_names(self):
        with self.assertRaises(live.Abort):
            live.create_session("agentreetest-a", "/a.kdl", live.Ownership(), sh=FakeSh())
        with self.assertRaises(live.Abort):
            live.require_ours("web-app")


def win(wid, title, argv, *, overridden=False, **user_vars):
    return {"id": wid, "title": title, "title_overridden": overridden, "user_vars": user_vars,
            "foreground_processes": [{"cmdline": argv}]}


def ls(*windows):
    return [{"id": 1, "tabs": [{"id": 1, "windows": list(windows)}]}]


class SelectWindowsTest(unittest.TestCase):
    ZJ = "/opt/homebrew/bin/zellij"

    def sel(self, *windows):
        return live.select_our_windows(ls(*windows), live.RUN, live.OURS)

    def test_marked_and_corroborated(self):
        a = live.A
        self.assertEqual(self.sel(win(5, f"{a} | fish", [self.ZJ, "attach", a], agentreetest_run=live.RUN)), [5])
        self.assertEqual(self.sel(win(6, f"{a} | fish", [self.ZJ, "attach", a], agentree_session=a)), [6])

    def test_client_switched_to_a_user_session(self):
        a = live.A
        self.assertEqual(self.sel(win(5, "web-app | nvim", [self.ZJ, "attach", a], agentreetest_run=live.RUN)), [])
        self.assertEqual(self.sel(win(5, "web-app | nvim", [self.ZJ, "attach", a], agentree_session=a)), [])

    def test_overridden_title_rejected(self):
        a = live.A
        self.assertEqual(self.sel(win(5, f"{a} | fish", [self.ZJ, "attach", a], overridden=True,
                                      agentreetest_run=live.RUN, agentree_session=a)), [])

    def test_default_title_only_for_fresh_marked_clients(self):
        a = live.A
        # freshly launched: kitty's default title, agentree_session var == argv session -> ours
        self.assertEqual(self.sel(win(5, "zellij", [self.ZJ, "attach", a], agentreetest_run=live.RUN, agentree_session=a)), [5])
        # run marker but no agentree_session var: cannot corroborate a default title
        self.assertEqual(self.sel(win(5, "zellij", [self.ZJ, "attach", a], agentreetest_run=live.RUN)), [])
        # var names a different session than argv
        self.assertEqual(self.sel(win(5, "zellij", [self.ZJ, "attach", a], agentree_session=live.B)), [])

    def test_unmarked_or_other_run(self):
        a = live.A
        self.assertEqual(self.sel(win(5, f"{a} | fish", [self.ZJ, "attach", a])), [])
        self.assertEqual(self.sel(win(5, f"{a} | fish", [self.ZJ, "attach", a], agentreetest_run="ffffff")), [])
        self.assertEqual(self.sel(win(5, "user | fish", [self.ZJ, "attach", "user"], agentree_session="user")), [])

    def test_not_a_zellij_client(self):
        self.assertEqual(self.sel(win(5, f"{live.A} | fish", ["fish"], agentreetest_run=live.RUN)), [])


if __name__ == "__main__":
    unittest.main()


class FakeClaudeTest(unittest.TestCase):
    """scripts/live_claude.py's stand-in: a compiled `claude` that records its argv and is seen by
    agentree's process scan as an interactive claude carrying our --session-id."""

    def test_fake_claude_records_argv_and_is_detected(self):
        import shutil
        import sys
        import tempfile
        import time

        if not shutil.which("cc"):
            self.skipTest("no C compiler")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            spec = importlib.util.spec_from_file_location("live_claude", os.path.join(ROOT, "scripts",
                                                                                      "live_claude.py"))
            lc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lc)
        finally:
            sys.path.remove(os.path.join(ROOT, "scripts"))
        from agentree.providers import claude as cc
        from agentree.sources import proc
        with tempfile.TemporaryDirectory() as work:
            path, rec = lc.make_fake_claude(work)
            self.assertEqual(os.path.basename(path), "claude")
            argv = [path, "--settings", "/s.json", "--session-id", "sid-1", "--", '-x "y"\nz']
            p = subprocess.Popen(argv)
            try:
                deadline = time.monotonic() + 5
                while lc.read_argv(rec) is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(lc.read_argv(rec), argv)
                live = proc.proc_args_env(p.pid)
                self.assertIsNotNone(live)
                self.assertTrue(cc.is_claude_argv(live[0]))
                self.assertEqual(cc.claude_session_arg(live[0]), "sid-1")
                self.assertIn(p.pid, cc.find_claude([(p.pid, os.getpid(), " ".join(argv))]))
            finally:
                p.kill()
                p.wait()
        self.assertIsNone(lc.read_argv("/nonexistent/x"))
