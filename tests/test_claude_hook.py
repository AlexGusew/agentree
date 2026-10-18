"""agentree/providers/claude.py (hook side): status records, settings file, launch args, setup step,
`agentree claude-hook`."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agentree.providers import claude
from agentree.providers import registry
from agentree.providers.base import LaunchRequest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "0b6c7f1e-1d2a-4c3b-9e8f-123456789abc"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        p = mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": self.state})
        p.start()
        self.addCleanup(p.stop)

    def hook(self, event, now, env=None, ppid=999, **payload):
        return claude.handle_hook({"session_id": SID, "hook_event_name": event, "cwd": "/w/x", **payload},
                                  env or {}, ppid, now=now)


class PathsTest(Base):
    def test_under_state_dir(self):
        self.assertEqual(claude.settings_path(), os.path.join(self.state, "claude", "settings.json"))
        self.assertEqual(claude.status_path(SID), os.path.join(self.state, "claude", "status", f"{SID}.json"))
        self.assertTrue(claude.settings_path().startswith(self.tmp.name))


class StatusMappingTest(Base):
    def test_lifecycle(self):
        env = {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "7", "CLAUDE_PID": "4321"}
        r = self.hook("SessionStart", 100.0, env, transcript_path="/t.jsonl", source="startup")
        self.assertEqual((r["status"], r["since"], r["pid"], r["zellij_session"], r["zellij_pane"]),
                         ("idle", 100.0, 4321, "S", 7))
        self.assertEqual((r["v"], r["session_id"], r["cwd"], r["transcript_path"]), (1, SID, "/w/x", "/t.jsonl"))
        r = self.hook("UserPromptSubmit", 101.0, prompt="go")
        self.assertEqual((r["status"], r["since"]), ("busy", 101.0))
        self.assertEqual((r["pid"], r["zellij_session"], r["zellij_pane"]), (4321, "S", 7))   # kept from before
        r = self.hook("PreToolUse", 102.0, tool_name="Bash")
        self.assertEqual((r["status"], r["since"], r["ts"]), ("busy", 101.0, 102.0))          # since unchanged
        r = self.hook("PostToolUse", 103.0)
        self.assertEqual((r["status"], r["since"]), ("busy", 101.0))
        r = self.hook("PermissionRequest", 104.0)
        self.assertEqual((r["status"], r["since"]), ("needs_input", 104.0))
        r = self.hook("Notification", 105.0, notification_type="permission_prompt", message="Claude needs your "
                      "permission to use Bash")
        self.assertEqual((r["status"], r["since"]), ("needs_input", 104.0))
        self.assertEqual(r["message"], "Claude needs your permission to use Bash")
        r = self.hook("Notification", 106.0, notification_type="idle_prompt", message="waiting")
        self.assertEqual(r["status"], "needs_input")                      # needs_input is kept
        self.assertEqual(r["message"], "Claude needs your permission to use Bash")
        r = self.hook("Stop", 107.0, last_assistant_message="  all done  ")
        self.assertEqual((r["status"], r["since"], r["last_message"], r["message"]), ("idle", 107.0, "all done", None))
        r = self.hook("Notification", 108.0, notification_type="idle_prompt")
        self.assertEqual((r["status"], r["since"]), ("idle", 107.0))
        r = self.hook("SessionEnd", 109.0, reason="exit")
        self.assertEqual(r["status"], "ended")
        self.assertEqual(claude.read_record(SID)["status"], "ended")
        self.assertIn(SID, claude.read_statuses())
        self.assertEqual(claude.read_statuses(max_age_s=10, now=200.0), {})

    def test_unknown_event_keeps_status(self):
        self.hook("UserPromptSubmit", 1.0)
        r = self.hook("PreCompact", 2.0)
        self.assertEqual((r["status"], r["since"], r["event"]), ("busy", 1.0, "PreCompact"))

    def test_pid_falls_back_to_parent(self):
        self.assertEqual(self.hook("SessionStart", 1.0, ppid=555)["pid"], 555)

    def test_long_text_trimmed(self):
        r = self.hook("Stop", 1.0, last_assistant_message="x" * 5000)
        self.assertEqual(len(r["last_message"]), claude.MAX_TEXT)

    def test_unsafe_session_ids_rejected(self):
        for sid in ("../evil", "a/b", ".hidden", "", "x" * 200, None, 42, "a b"):
            self.assertIsNone(claude.handle_hook({"session_id": sid, "hook_event_name": "Stop"}, {}, 1, now=1.0), sid)
        self.assertIsNone(claude.handle_hook({"session_id": SID}, {}, 1, now=1.0))      # no event name
        self.assertFalse(os.path.exists(os.path.join(self.state, "claude", "status")))
        self.assertFalse(os.path.exists(os.path.join(self.state, "evil.json")))
        self.assertIsNone(claude.read_record("../x"))

    def test_prune(self):
        self.hook("Stop", 1.0)
        p = claude.status_path(SID)
        os.utime(p, (1000.0, 1000.0))
        self.assertEqual(claude.prune(max_age_s=10, now=1005.0), 0)
        self.assertEqual(claude.prune(max_age_s=10, now=2000.0), 1)
        self.assertFalse(os.path.exists(p))


class HookMainTest(Base):
    def run_main(self, text, env=None):
        return claude.hook_main(io.StringIO(text), env or {})

    def test_writes_record(self):
        self.assertEqual(self.run_main(json.dumps({"session_id": SID, "hook_event_name": "UserPromptSubmit"}),
                                       {"ZELLIJ_PANE_ID": "3"}), 0)
        rec = claude.read_record(SID)
        self.assertEqual((rec["status"], rec["zellij_pane"]), ("busy", 3))

    def test_garbage_is_ignored(self):
        for text in ("", "   ", "not json", "[1, 2]", '{"session_id": 1}', "\x00\xff", '{"hook_event_name": "Stop"}'):
            self.assertEqual(self.run_main(text), 0, text)
        self.assertFalse(os.path.exists(os.path.join(self.state, "claude")))

    def test_unwritable_state_still_exits_0(self):
        with open(os.path.join(self.tmp.name, "file"), "w") as fh:
            fh.write("x")
        with mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": os.path.join(self.tmp.name, "file", "sub")}):
            self.assertEqual(self.run_main(json.dumps({"session_id": SID, "hook_event_name": "Stop"})), 0)


class CliTest(Base):
    """`python3 -m agentree claude-hook` as Claude runs it: JSON on stdin, exit 0, record written."""

    def cli(self, stdin, extra_env=None):
        env = dict(os.environ, AGENTREE_STATE_DIR=self.state, PYTHONPATH=ROOT, **(extra_env or {}))
        return subprocess.run([sys.executable, "-m", "agentree", "claude-hook"], input=stdin, capture_output=True,
                              text=True, env=env, cwd=self.tmp.name, timeout=30)

    def test_cli_writes_status(self):
        payload = {"session_id": SID, "hook_event_name": "SessionStart", "cwd": "/w/x", "source": "startup"}
        r = self.cli(json.dumps(payload), {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "12"})
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        rec = claude.read_record(SID)
        self.assertEqual((rec["status"], rec["cwd"], rec["zellij_session"], rec["zellij_pane"]),
                         ("idle", "/w/x", "S", 12))
        self.assertIsInstance(rec["pid"], int)                           # the hook's parent (no CLAUDE_PID)
        r = self.cli(json.dumps({**payload, "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(claude.read_record(SID)["status"], "busy")

    def test_cli_garbage_exit_0(self):
        for text in ("garbage{", ""):
            r = self.cli(text)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), text)
        self.assertFalse(os.path.exists(os.path.join(self.state, "claude", "status")))

    def test_dispatch_in_run(self):
        from agentree import cli
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": SID, "hook_event_name": "Stop"}))):
            self.assertEqual(cli.run(["claude-hook"]), 0)
        self.assertEqual(claude.read_record(SID)["status"], "idle")


class SettingsTest(Base):
    def test_settings_doc(self):
        doc = claude.settings_doc("/opt/agentree claude-hook")
        self.assertEqual(sorted(doc), ["hooks"])
        self.assertEqual(sorted(doc["hooks"]), sorted(claude.HOOK_EVENTS))
        for ev, entries in doc["hooks"].items():
            self.assertEqual(len(entries), 1)
            h = entries[0]["hooks"]
            self.assertEqual(h, [{"type": "command", "command": "/opt/agentree claude-hook", "timeout": 10}])
            self.assertEqual("matcher" in entries[0], ev in ("PreToolUse", "PostToolUse", "PermissionRequest",
                                                              "Notification"))

    def test_write_settings_idempotent(self):
        p = claude.write_settings("/opt/agentree claude-hook")
        self.assertEqual(p, claude.settings_path())
        with open(p) as fh:
            first = fh.read()
        ino, mtime = os.stat(p).st_ino, os.stat(p).st_mtime_ns
        self.assertEqual(claude.write_settings("/opt/agentree claude-hook"), p)
        self.assertEqual((os.stat(p).st_ino, os.stat(p).st_mtime_ns), (ino, mtime))   # unchanged: not rewritten
        claude.write_settings("/other/agentree claude-hook")
        with open(p) as fh:
            self.assertNotEqual(fh.read(), first)
        self.assertEqual([n for n in os.listdir(os.path.dirname(p)) if n.endswith(".tmp")], [])

    def test_hook_command(self):
        with mock.patch.dict(os.environ, {"AGENTREE_SELF": "/opt/my agentree"}):
            self.assertEqual(claude.self_command(), ["/opt/my", "agentree"])
            self.assertEqual(claude.hook_command(), "/opt/my agentree claude-hook")
        with mock.patch.dict(os.environ, {"AGENTREE_SELF": ""}), mock.patch.object(sys, "argv", ["/nonexistent/x.py"]):
            cmd = claude.hook_command()
            self.assertTrue(cmd.startswith("PYTHONPATH="), cmd)
            self.assertTrue(cmd.endswith("-m agentree claude-hook"), cmd)
            # the import root of the agentree package (here: the source checkout), not a dirname count
            self.assertEqual(claude.import_root(), ROOT)
            self.assertTrue(cmd.startswith(f"PYTHONPATH={ROOT} ") or cmd.startswith(f"PYTHONPATH='{ROOT}' "), cmd)

    def test_hook_command_from_zipapp(self):
        """Built zipapp: the import root is the archive itself, and both hook command forms
        (the executable zipapp, `PYTHONPATH=<zipapp> python -m agentree`) write a status record."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            import build
        finally:
            sys.path.remove(os.path.join(ROOT, "scripts"))
        app = build.build(os.path.join(self.tmp.name, "dist", "agentree"), python=sys.executable, pyc=False)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "AGENTREE_SELF")}
        env["AGENTREE_STATE_DIR"] = self.state
        code = ("import sys; from agentree.providers import claude as c; "
                "print(c.import_root()); print(c.hook_command())")
        r = subprocess.run([sys.executable, "-c", code], env={**env, "PYTHONPATH": app}, capture_output=True,
                           text=True, cwd=self.tmp.name, timeout=30)
        root, cmd = r.stdout.splitlines()[:2]
        self.assertEqual(root, app, r.stderr)
        self.assertEqual(cmd, f"PYTHONPATH={app} {sys.executable} -m agentree claude-hook")
        for i, argv in enumerate(([sys.executable, "-m", "agentree", "claude-hook"], [app, "claude-hook"])):
            sid = f"zip-{i}"
            r = subprocess.run(argv, input=json.dumps({"session_id": sid, "hook_event_name": "Stop"}),
                               env={**env, "PYTHONPATH": app} if i == 0 else env, capture_output=True, text=True,
                               cwd=self.tmp.name, timeout=30)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""), argv)
            self.assertEqual((claude.read_record(sid) or {}).get("status"), "idle", argv)


class LaunchArgsTest(Base):
    def test_launch_args(self):
        s = claude.settings_path()
        self.assertEqual(claude.launch_args("sid"), ["--settings", s, "--session-id", "sid"])
        self.assertEqual(claude.launch_args(None), ["--settings", s])
        self.assertEqual(claude.launch_args("sid", "fix it", settings="/x"),
                         ["--settings", "/x", "--session-id", "sid", "fix it"])
        self.assertEqual(claude.launch_args("sid", "-v please")[-2:], ["--", "-v please"])
        self.assertEqual(claude.launch_args("sid", "update")[-1], "update ")      # not the subcommand
        self.assertEqual(claude.launch_args("sid", "")[-1], "sid")

    def test_launch_argv_is_a_claude_session(self):
        for prompt in ("fix it", "-v", "update", "mcp", None):
            argv = ["/u/.local/bin/claude", *claude.launch_args(SID, prompt)]
            self.assertTrue(claude.is_claude_argv(argv), argv)
            self.assertEqual(claude.claude_session_arg(argv), SID)

    def test_new_session_id(self):
        a, b = claude.new_session_id(), claude.new_session_id()
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")


class _Cfg:
    def __init__(self, cmd):
        self.cmd = cmd

    def provider_command(self, pid):
        return self.cmd if pid == "claude" else "opencode"


class _Runner:
    def __init__(self, dry_run):
        self.dry_run, self.acted = dry_run, []

    def internal(self, description, fn):
        self.acted.append(description)
        return None if self.dry_run else fn()


class ProviderLaunchTest(Base):
    def setUp(self):
        super().setUp()
        self.prov = claude.ClaudeProvider(registry.spec("claude"))
        self.exe = os.path.join(self.tmp.name, "claude")
        with open(self.exe, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.exe, 0o755)

    def test_build_launch_is_read_only(self):
        req = LaunchRequest(mode="spawn", prompt="fix it", session_id=SID)
        la = self.prov.build_launch(_Cfg(f"{self.exe} --model opus"), req)
        s = claude.settings_path()
        self.assertEqual(la.argv, (self.exe, "--model", "opus", "--settings", s, "--session-id", SID, "fix it"))
        self.assertEqual((la.session_id, la.errors, la.warnings), (SID, (), ()))
        self.assertEqual(la.setup, (claude.settings_step(s),))
        self.assertEqual(la.setup[0].payload, (("settings_path", s),))
        self.assertFalse(os.path.exists(os.path.join(self.state, "claude")))          # nothing written
        # pane: a fresh session id when none is given; no prompt
        la = self.prov.build_launch(_Cfg(self.exe), LaunchRequest(mode="pane"))
        self.assertEqual(la.argv[:4], (self.exe, "--settings", s, "--session-id"))
        self.assertEqual((len(la.argv), la.argv[4]), (5, la.session_id))
        # spawn without a prompt: only for the "agent" layout
        la = self.prov.build_launch(_Cfg(self.exe), LaunchRequest(mode="spawn"))
        self.assertIsNone(la.argv)
        self.assertEqual(la.warnings, ("no prompt and no agent layout: claude is not started (choose layout agent)",))
        la = self.prov.build_launch(_Cfg(self.exe), LaunchRequest(mode="spawn", layout="agent", session_id=SID))
        self.assertEqual(la.argv, (self.exe, "--settings", s, "--session-id", SID))

    def test_build_launch_missing_command(self):
        missing = os.path.join(self.tmp.name, "nope")
        la = self.prov.build_launch(_Cfg(missing), LaunchRequest(mode="pane"))
        self.assertIsNone(la.argv)
        self.assertEqual(la.errors, (registry.missing_command_message("claude", _Cfg(missing)),))
        la = self.prov.build_launch(_Cfg(missing), LaunchRequest(mode="spawn", prompt="p", session_id=SID))
        self.assertEqual(la.argv[:2], (missing, "--settings"))                       # spawn: plan error, argv kept
        self.assertEqual(len(la.errors), 1)

    def test_execute_setup_honours_dry_run(self):
        step = claude.settings_step(os.path.join(self.tmp.name, "hooks", "settings.json"))
        run = _Runner(dry_run=True)
        self.assertEqual(self.prov.execute_setup(step, runner=run, state_dir=self.state), "")
        self.assertEqual(run.acted, [f"write claude hook settings {dict(step.payload)['settings_path']}"])
        self.assertFalse(os.path.exists(dict(step.payload)["settings_path"]))
        run = _Runner(dry_run=False)
        self.prov.execute_setup(step, runner=run, state_dir=self.state)
        with open(dict(step.payload)["settings_path"]) as fh:
            self.assertEqual(sorted(json.load(fh)["hooks"]), sorted(claude.HOOK_EVENTS))
        with self.assertRaises(ValueError):
            self.prov.execute_setup(claude.SetupStep("nope", "x"), runner=run, state_dir=self.state)

    def test_run_internal(self):
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": SID, "hook_event_name": "Stop"}))):
            self.assertEqual(self.prov.run_internal("claude-hook", []), 0)
        self.assertEqual(claude.read_record(SID)["status"], "idle")
        with self.assertRaises(KeyError):
            self.prov.run_internal("other", [])


if __name__ == "__main__":
    unittest.main()
