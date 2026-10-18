"""Single instance per zellij session: fcntl lock claim, record, verified hand-off."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agentree import cli
from agentree.actions import exec as ex
from agentree.actions.runner import identity_key

ZJ = "/opt/homebrew/bin/zellij"


class _Tty:
    def isatty(self):
        return True


HOLDER = r"""
import os, sys, time
from agentree import cli
inst, running = cli.claim_instance()
if inst is not None and "--complete" in sys.argv:
    inst.complete()
print("claimed" if inst else "busy", flush=True)
sys.stdin.read()          # hold until the test closes stdin (or kills us)
"""


class InstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"ZELLIJ_SESSION_NAME": "sess/1", "ZELLIJ_PANE_ID": "7", "ZELLIJ_SOCKET_DIR": "/tmp/zellij-test",
                    "AGENTREE_STATE_DIR": self.tmp.name}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)

    def holder(self, complete=True, **env):
        """Another process holding the claim for this session (its env = this session's)."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        args = [sys.executable, "-c", HOLDER] + (["--complete"] if complete else [])
        p = subprocess.Popen(args, env={**os.environ, "PYTHONPATH": root, **env},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.stdin.close(), p.stdout.close()))
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "claimed")
        return p

    def test_paths_use_hashed_identity(self):
        lock, rec = cli.instance_paths("/tmp/zellij-test/", "sess/1")
        key = identity_key("/private/tmp/zellij-test", "sess/1")
        self.assertEqual((lock, rec), (os.path.join(self.tmp.name, f"instance-{key}.lock"),
                                       os.path.join(self.tmp.name, f"instance-{key}.json")))

    def test_claim_record_and_release(self):
        inst, running = cli.claim_instance()
        self.assertIsNone(running)
        rec = cli.read_instance(inst.record_path)
        self.assertEqual(rec["pid"], os.getpid())
        self.assertEqual((rec["session"], rec["socket_dir"], rec["pane_id"]),
                         ("sess/1", os.path.realpath("/tmp") + "/zellij-test", "7"))
        inst.complete()
        start = cli.read_instance(inst.record_path)["start"]
        self.assertTrue(time.time() - 3600 < start <= time.time())
        inst.release()
        self.assertFalse(os.path.exists(inst.record_path))
        inst2, running = cli.claim_instance()          # lock released -> claimable again
        self.assertIsNotNone(inst2)
        inst2.release()

    def test_outside_zellij(self):
        self.assertEqual(cli.claim_instance({"AGENTREE_STATE_DIR": self.tmp.name}), (None, None))

    def test_lock_is_the_claim(self):
        p = self.holder()
        inst, running = cli.claim_instance()
        self.assertIsNone(inst)
        self.assertEqual(running["pid"], p.pid)
        # a different session (or the same name in another socket dir) is independent
        other = dict(self.env, ZELLIJ_SOCKET_DIR="/tmp/zellij-other")
        inst2, running2 = cli.claim_instance(other)
        self.assertIsNotNone(inst2)
        inst2.release()
        # holder dies -> lock released by the kernel -> claimable (no stale-pid logic needed)
        p.kill()
        p.wait()
        inst3, _ = cli.claim_instance()
        self.assertIsNotNone(inst3)
        inst3.release()

    def test_stale_record_without_lock_is_ignored(self):
        lock, rec = cli.instance_paths("/tmp/zellij-test", "sess/1")
        with open(rec, "w") as fh:
            json.dump({"pid": 1, "pane_id": "3", "session": "sess/1"}, fh)
        inst, running = cli.claim_instance()
        self.assertIsNone(running)
        self.assertEqual(cli.read_instance(rec)["pid"], os.getpid())
        inst.release()

    # ---------------------------------------------------------------- hand-off
    def fake(self, panes, clients):
        calls = []

        def run(argv, env=None, timeout=1.0, log=False, dry_run=False):
            calls.append((tuple(argv[1:]), dict(env or {})))
            if "list-panes" in argv:
                return ex.Result(tuple(argv), 0, json.dumps(panes), "", False, 0.0)
            if "list-clients" in argv:
                rows = "".join(f"{i}  terminal_1  x\n" for i in range(clients))
                return ex.Result(tuple(argv), 0, "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n" + rows, "", False, 0.0)
            return ex.Result(tuple(argv), 0, "", "", False, 0.0)
        return calls, run

    PANES = [{"id": 7, "is_plugin": False, "tab_id": 4, "exited": False},
             {"id": 7, "is_plugin": True, "tab_id": 0}]

    def failed_claim(self, **holder_env):
        p = self.holder(**holder_env)
        inst, running = cli.claim_instance()
        self.assertIsNone(inst)
        self.assertEqual(running["pid"], p.pid)
        return p, running

    @staticmethod
    def focused(calls):
        return [c for c in calls if c[0][3] in ("go-to-tab-by-id", "focus-pane-id")]

    def test_hand_off_focuses_verified_holder_explicitly(self):
        p, rec = self.failed_claim()
        calls, run = self.fake(self.PANES, 1)
        msg, inst = cli.hand_off(rec, run=run)               # real pid/start/env checks
        self.assertIsNone(inst)
        self.assertIn("focused", msg)
        env = {"ZELLIJ_SOCKET_DIR": "/tmp/zellij-test"}
        self.assertEqual(calls, [
            (("--session", "sess/1", "action", "list-panes", "--state", "--json"), env),
            (("--session", "sess/1", "action", "list-clients"), env),
            (("--session", "sess/1", "action", "go-to-tab-by-id", "4"), env),
            (("--session", "sess/1", "action", "focus-pane-id", "terminal_7"), env)])

    def test_holder_exits_between_claim_and_hand_off(self):
        p, rec = self.failed_claim()
        p.kill()
        p.wait()
        calls, run = self.fake(self.PANES, 1)
        msg, inst = cli.hand_off(rec, run=run)
        self.assertIsNotNone(inst)                           # we are the instance now
        self.assertEqual(calls, [])
        self.assertIn("taking over", msg)
        self.assertEqual(cli.read_instance(inst.record_path)["pid"], os.getpid())
        inst.release()

    def test_pid_or_start_mismatch(self):
        p, rec = self.failed_claim()
        calls, run = self.fake(self.PANES, 1)
        lock, path = cli.instance_paths("/tmp/zellij-test", "sess/1")
        with open(path, "w") as fh:                           # record rewritten with a wrong start
            json.dump(dict(rec, start=rec["start"] - 5), fh)
        msg, inst = cli.hand_off(rec, run=run)
        self.assertIsNone(inst)
        self.assertIn("different process", msg)
        with open(path, "w") as fh:                           # pid reused by another process
            json.dump(dict(rec, pid=os.getpid()), fh)
        msg, inst = cli.hand_off(rec, run=run)
        self.assertIn("different process", msg)
        with open(path, "w") as fh:                           # pid gone
            json.dump(dict(rec, pid=2 ** 22 + 7), fh)
        msg, inst = cli.hand_off(rec, run=run)
        self.assertIn("is gone", msg)
        self.assertEqual(calls, [])

    def test_start_missing_holder_still_starting(self):
        p = self.holder(complete=False)
        inst, rec = cli.claim_instance()
        self.assertIsNone(rec.get("start"))
        calls, run = self.fake(self.PANES, 1)
        msg, inst = cli.hand_off(rec, run=run)
        self.assertIsNone(inst)
        self.assertIn("still starting", msg)
        self.assertEqual(calls, [])

    def test_pane_belongs_to_another_process(self):
        # the holder's real env says pane 9, the record (forged / stale) says pane 7
        p, rec = self.failed_claim(ZELLIJ_PANE_ID="9")
        lock, path = cli.instance_paths("/tmp/zellij-test", "sess/1")
        with open(path, "w") as fh:
            json.dump(dict(rec, pane_id="7"), fh)
        calls, run = self.fake(self.PANES, 1)
        msg, inst = cli.hand_off(rec, run=run)
        self.assertIn("environment does not match", msg)
        self.assertEqual(calls, [])
        # holder in another socket dir under the same name: env mismatch as well
        msg, inst = cli.hand_off(rec, run=run, proc_env=lambda pid: {
            "ZELLIJ_SESSION_NAME": "sess/1", "ZELLIJ_PANE_ID": "7", "ZELLIJ_SOCKET_DIR": "/tmp/zellij-other"})
        self.assertIn("environment does not match", msg)

    def test_exited_or_missing_pane(self):
        p, rec = self.failed_claim()
        for panes, why in ([dict(self.PANES[0], exited=True)], "exited"), ([], "not in sess/1"):
            calls, run = self.fake(panes, 1)
            msg, inst = cli.hand_off(rec, run=run)
            self.assertIn(why, msg)
            self.assertEqual(self.focused(calls), [])

    def test_hand_off_record_and_client_refusals(self):
        p, rec = self.failed_claim()
        for bad, clients, why in [
            (dict(rec, session="other"), 1, "already running"),
            (dict(rec, socket_dir="/tmp/elsewhere"), 1, "already running"),
            (dict(rec, pane_id=None), 1, "already running"),
            (rec, 2, "2 clients"),
            (rec, 0, "0 clients"),
        ]:
            lock, path = cli.instance_paths("/tmp/zellij-test", "sess/1")
            with open(path, "w") as fh:
                json.dump(bad, fh)
            calls, run = self.fake(self.PANES, clients)
            msg, inst = cli.hand_off(bad, run=run)
            self.assertIn(why, msg)
            self.assertEqual(self.focused(calls), [], (bad, msg))

    def test_hand_off_dry_run_never_focuses(self):
        p, rec = self.failed_claim()
        calls, run = self.fake(self.PANES, 1)
        msg, inst = cli.hand_off(rec, dry_run=True, run=run)
        self.assertIn("dry-run", msg)
        self.assertIsNone(inst)
        self.assertEqual(calls, [])

    def test_tui_hands_off_before_rendering(self):
        p = self.holder()
        with mock.patch.object(sys, "stdin", _Tty()), mock.patch.object(sys, "stdout", _Tty()), \
                mock.patch.object(cli, "hand_off", return_value=("focused", None)) as ho, \
                mock.patch("agentree.ui.app.run_app", side_effect=AssertionError("rendered")):
            t0 = time.perf_counter()
            self.assertEqual(cli.run(["--dry-run"]), 0)
            self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(ho.call_args[0][0]["pid"], p.pid)
        self.assertTrue(ho.call_args[1]["dry_run"])

    def test_tui_takes_over_when_holder_vanished(self):
        p = self.holder()
        seen = {}
        real_hand_off = cli.hand_off

        def vanish_then_hand_off(rec, **kw):
            p.kill()
            p.wait()
            return real_hand_off(rec, **kw)

        with mock.patch.object(sys, "stdin", _Tty()), mock.patch.object(sys, "stdout", _Tty()), \
                mock.patch.object(cli, "hand_off", vanish_then_hand_off), \
                mock.patch("agentree.ui.app.run_app", lambda **kw: seen.setdefault("ran", True) and 0):
            self.assertEqual(cli.run([]), 0)
        self.assertTrue(seen.get("ran"))                     # proceeded as the new instance
        inst, _ = cli.claim_instance()                       # and released the lock on exit
        self.assertIsNotNone(inst)
        inst.release()

    def test_tui_claims_and_releases(self):
        seen = {}

        def fake_run_app(**kw):
            lock, rec = cli.instance_paths("/tmp/zellij-test", "sess/1")
            seen["rec"] = cli.read_instance(rec)
            kw["on_first_frame"]()                # completes the record (start time)
            seen["rec2"] = cli.read_instance(rec)
            return 0

        with mock.patch.object(sys, "stdin", _Tty()), mock.patch.object(sys, "stdout", _Tty()), \
                mock.patch("agentree.ui.app.run_app", fake_run_app):
            self.assertEqual(cli.run([]), 0)
        self.assertEqual(seen["rec"]["pid"], os.getpid())
        self.assertIsNotNone(seen["rec2"]["start"])
        lock, rec = cli.instance_paths("/tmp/zellij-test", "sess/1")
        self.assertFalse(os.path.exists(rec))
        inst, _ = cli.claim_instance()            # lock released
        self.assertIsNotNone(inst)
        inst.release()

    def test_proc_start_time(self):
        p = subprocess.Popen(["sleep", "5"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        t = cli.proc_start_time(p.pid)
        self.assertAlmostEqual(t, time.time(), delta=3)
        self.assertLess(cli.proc_start_time(os.getpid()), t)

    def test_make_actions_is_real(self):
        from agentree.actions.real import RealActions
        a = cli.make_actions(None, dry_run=True)
        self.assertIsInstance(a, RealActions)
        self.assertTrue(a.dry_run)


if __name__ == "__main__":
    unittest.main()
