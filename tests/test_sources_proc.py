import os
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

from agentree.providers import claude as cc
from agentree.providers import opencode as oc
from agentree.providers import registry
from agentree.sources import proc as p

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")


def fx(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class ParsersTest(unittest.TestCase):
    def test_procargs2(self):
        buf = (struct.pack("=i", 3) + b"/bin/exe\0\0\0\0" + b"exe\0--port\0" + b"5004\0"
               + b"A=1\0ZELLIJ_PANE_ID=2\0EQ=a=b\0\0garbage\0")
        argv, env = p.parse_procargs2(buf)
        self.assertEqual(argv, ("exe", "--port", "5004"))
        self.assertEqual(env, {"A": "1", "ZELLIJ_PANE_ID": "2", "EQ": "a=b"})

    def test_procargs2_bad(self):
        self.assertIsNone(p.parse_procargs2(b""))
        self.assertIsNone(p.parse_procargs2(struct.pack("=i", 1) + b"noterm"))

    def test_ps(self):
        rows = p.parse_ps("  1     0 /sbin/launchd\n123 1 /bin/zsh -c x y\nbad line\n77 1\n")
        self.assertEqual(rows, [(1, 0, "/sbin/launchd"), (123, 1, "/bin/zsh -c x y"), (77, 1, "")])

    def test_live_ps_opencode(self):
        rows = p.parse_ps(fx("ps_opencode.txt"))
        self.assertTrue(rows)
        self.assertEqual(sorted(oc.find_opencode(rows)), sorted(r[0] for r in rows))

    def test_lsof_listen_live(self):
        ports = p.parse_lsof_listen(fx("lsof_listen.txt"))
        self.assertTrue(ports)
        for pid, ps in ports.items():
            self.assertTrue(ps)
            self.assertTrue(all(5000 <= x < 6000 for x in ps))

    def test_lsof_listen_synthetic(self):
        text = "p10\nf11\nn127.0.0.1:5004\nf12\nn[::1]:5004\nf13\nn*:9229\np11\n"
        self.assertEqual(p.parse_lsof_listen(text), {10: [5004, 9229], 11: []})

    def test_lsof_cwd_live(self):
        cw = p.parse_lsof_fpn(fx("lsof_cwd.txt"))
        self.assertTrue(cw)
        for names in cw.values():
            self.assertTrue(names[0].startswith("/"))

    def test_is_opencode_argv(self):
        self.assertTrue(oc.is_opencode_argv(("/nix/store/x/bin/opencode", "--port", "5000", "--auto")))
        self.assertTrue(oc.is_opencode_argv(("opencode",)))
        self.assertTrue(oc.is_opencode_argv(("/x/.opencode", "serve")))
        self.assertTrue(oc.is_opencode_argv(("opencode", "/some/dir")))
        self.assertFalse(oc.is_opencode_argv(("opencode", "run", "hi")))
        self.assertFalse(oc.is_opencode_argv(("opencode", "auth", "login")))
        self.assertFalse(oc.is_opencode_argv(("opencode-auto",)))
        self.assertFalse(oc.is_opencode_argv(("node", "opencode")))
        self.assertFalse(oc.is_opencode_argv(()))

    def test_find_opencode_excludes_self(self):
        rows = [(os.getpid(), 1, "opencode"), (5, 1, "/bin/opencode --port 1"), (6, 1, "/x/agentree opencode")]
        self.assertEqual(oc.find_opencode(rows), [5])


class StartTimeTest(unittest.TestCase):
    def test_parse_kinfo_start(self):
        buf = struct.pack("=qi", 1_791_504_414, 211_443) + bytes(640)
        self.assertAlmostEqual(p.parse_kinfo_start(buf), 1_791_504_414.211443, places=6)
        self.assertIsNone(p.parse_kinfo_start(b""))
        self.assertIsNone(p.parse_kinfo_start(bytes(648)))                      # sec 0: no process
        self.assertIsNone(p.parse_kinfo_start(struct.pack("=qi", 5, 2_000_000) + bytes(640)))

    def test_live(self):
        import time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            t = p.start_time(child.pid)
            self.assertIsNotNone(t)
            self.assertLess(abs(t - time.time()), 5)
            self.assertEqual(p.start_time(child.pid), t)                         # stable
            self.assertLess(p.start_time(os.getpid()), t)                        # we started earlier
            self.assertEqual(p.start_times([child.pid, os.getpid(), 999_999]),
                             {child.pid: t, os.getpid(): p.start_time(os.getpid())})
        finally:
            child.kill()
            child.wait()
        self.assertIsNone(p.start_time(child.pid))                               # reaped: gone
        self.assertIsNone(p.start_time(999_999))

    def test_matches_runner_helper(self):
        from agentree.actions.runner import proc_start_time
        self.assertEqual(p.start_time(os.getpid()), proc_start_time(os.getpid()))


class ClaudeArgvTest(unittest.TestCase):
    def test_is_claude_argv(self):
        yes = [
            ("claude",),
            ("/Users/me/.local/bin/claude", "--settings", "/s.json", "--session-id", "abc"),
            ("/Users/me/.local/share/claude/versions/2.1.3",),
            ("node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "--model", "opus"),
            ("claude", "fix the tests"),
            ("claude", "--model", "mcp"),                       # flag value is not a subcommand
            ("claude", "--", "update"),                         # after --: the prompt
            ("claude", "--", "-p looks like a flag"),
            ("claude", "update "),                              # launch_args pads subcommand-like prompts
            ("claude", "-c"),
        ]
        no = [
            (), ("opencode",), ("vim", "claude"), ("claude", "-p", "hi"), ("claude", "--print", "x"),
            ("claude", "--version"), ("claude", "mcp", "list"), ("claude", "doctor"), ("claude", "update"),
            ("claude", "--settings", "/s", "config", "list"), ("claude", "agents"),
            ("node", "/x/other/cli.js"),
        ]
        claude, opencode = registry.get("claude"), registry.get("opencode")
        for argv in yes:
            self.assertTrue(cc.is_claude_argv(argv), argv)
            self.assertTrue(claude.matches_argv(argv), argv)
            self.assertFalse(opencode.matches_argv(argv), argv)
        for argv in no:
            self.assertFalse(cc.is_claude_argv(argv), argv)
            self.assertFalse(claude.matches_argv(argv), argv)
        self.assertTrue(cc.CLAUDE_SUBCOMMANDS >= {"mcp", "doctor", "update", "config"})
        self.assertEqual(cc.CLAUDE_SUBCOMMANDS, cc.SUBCOMMANDS)
        self.assertFalse(hasattr(p, "is_claude_argv") or hasattr(p, "find_opencode"))   # moved to providers

    def test_session_arg(self):
        self.assertEqual(cc.claude_session_arg(("claude", "--session-id", "u-1", "hi")), "u-1")
        self.assertEqual(cc.claude_session_arg(("claude", "--session-id=u-2")), "u-2")
        self.assertIsNone(cc.claude_session_arg(("claude", "hi")))
        self.assertIsNone(cc.claude_session_arg(("opencode", "--session-id", "x")))
        self.assertEqual(registry.get("claude").session_id_from_argv(("claude", "--session-id", "u-3")), "u-3")
        self.assertEqual(registry.get("opencode").session_id_from_argv(
            ("opencode", "attach", "http://127.0.0.1:4096", "--session", "ses_1")), "ses_1")
        self.assertIsNone(registry.get("opencode").session_id_from_argv(("opencode", "--port", "1")))

    def test_find_claude_uses_real_argv_and_drops_nested(self):
        rows = [
            (10, 1, "claude update the README"),                # ps split a prompt into a fake subcommand
            (11, 1, "claude mcp serve"),                        # a real subcommand
            (12, 10, "claude"),                                 # nested in pid 10: dropped
            (13, 1, "/bin/zsh"),
            (14, 1, "claude --settings /s --session-id abc"),   # argv unreadable: ps fallback
            (os.getpid(), 1, "claude"),                         # never ourselves
        ]
        real = {10: (("claude", "update the README"), {}), 11: (("claude", "mcp", "serve"), {}),
                12: (("claude",), {})}
        calls = []

        def args_env(pid):
            calls.append(pid)
            return real.get(pid)
        with mock.patch.object(p, "proc_args_env", side_effect=args_env):
            self.assertEqual(cc.find_claude(rows), [10, 14])
        self.assertNotIn(13, calls)                             # only claude executables are re-read
        procs = [p.Proc(20, 1, ("claude", "hi")), p.Proc(21, 20, ("claude",)), p.Proc(22, 1, ("opencode",))]
        self.assertEqual(cc.find_claude(procs), [20])


class LiveTest(unittest.TestCase):
    """Same-user processes are always readable; verify ctypes paths against lsof/ps."""

    def test_self_args_env(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)", "x y"],
                                 env=dict(os.environ, AGENTREE_PROBE="42"))
        try:
            deadline = time.monotonic() + 2        # Linux: /proc/<pid>/cmdline is empty until exec is done
            while (ae := p.proc_args_env(child.pid)) is None and time.monotonic() < deadline:
                time.sleep(0.01)
            argv, env = ae
            self.assertEqual(argv[-1], "x y")
            self.assertEqual(env.get("AGENTREE_PROBE"), "42")
        finally:
            child.kill()
            child.wait()

    def test_unreadable(self):
        self.assertIsNone(p.proc_args_env(1))  # launchd: root-owned
        self.assertIsNone(p.proc_args_env(999_999))

    def test_cwd_libproc_matches_lsof(self):
        pids = [os.getpid()]
        a = p.proc_cwd(pids)
        b = p.proc_cwd(pids, use_libproc=False)
        self.assertEqual(a[os.getpid()], os.getcwd())
        self.assertEqual(a, b)

    def test_struct_size(self):
        self.assertEqual(p.PROC_VNODEPATHINFO_SIZE, 2352)

    def test_list_procs(self):
        rows = p.list_procs()
        self.assertIn(os.getpid(), {r[0] for r in rows})


if __name__ == "__main__":
    unittest.main()
