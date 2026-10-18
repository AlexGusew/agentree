"""pty integration: run_app in a child process on a real pseudo-terminal."""
import fcntl
import json
import os
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r"""
import json, sys, time, dataclasses
from agentree.actions.api import NullActions
from agentree.config import Config
from agentree.model.sample import sample_snapshot
from agentree.types import UiState
from agentree.ui import app

out = sys.argv[1]
mode = sys.argv[2] if len(sys.argv) > 2 else ""
snap = dataclasses.replace(sample_snapshot(time.time()), stale=True)
saved = {}

def save_ui(ui):
    saved["ui"] = {"mode": ui.worktree_mode, "panes": ui.show_panes, "text": ui.text, "sel": ui.selected}

def refresh():
    if mode == "slow":
        # a refresh stuck in a subprocess (like a hung git/zellij call)
        from agentree.actions import exec as ex
        ex.run(["/bin/sleep", "5"], timeout=10)
    time.sleep(0.05)
    return dataclasses.replace(sample_snapshot(time.time()), stale=False)

class Bench(Exception):
    pass

from agentree.actions.api import ActionResult, Plan, ProgressEvent

class Cooperative(NullActions):
    # x -> execute waits for cancel, then rolls back (reporting progress) and returns
    def plan_remove(self, row, snap):
        return Plan("remove-worktree", None, (("git", "worktree", "remove", "x"),))

    def execute(self, plan, typed=None, *, progress=None, cancel=None):
        saved["started"] = True
        progress(ProgressEvent(0, 1, "git worktree remove", "running"))
        if cancel.wait(10):
            time.sleep(0.2)
            progress(ProgressEvent(0, 1, "git worktree remove", "rolled_back", "restored"))
            saved["rolled_back"] = True
            return ActionResult(False, "cancelled, rolled back")
        return ActionResult(True, "removed")

def first():
    if mode == "bench":
        raise Bench()

try:
    acts = Cooperative() if mode == "exec" else NullActions()
    rc = app.run_app(cfg=Config(), snapshot=snap, ui=UiState(), refresh=refresh, actions=acts,
                     save_ui=save_ui, on_first_frame=first)
except Bench:
    rc = 7
saved["rc"] = rc
saved["first_frame_ms"] = app.TIMINGS.get("first_frame_ms")
if mode == "slow":
    from agentree.actions import exec as ex
    saved["children"] = [p.pid for p in ex._procs.values()]
    ex.shutdown()               # what agentree.cli does after run_app returns
with open(out, "w") as fh:
    json.dump(saved, fh)
sys.exit(rc)
"""


def _winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtyTest(unittest.TestCase):
    def run_child(self, keys, cols=120, rows=32, extra_env=None, mode="", signal_after=None):
        master, slave = os.openpty()
        _winsize(slave, cols, rows)
        result = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        result.close()
        env = dict(os.environ, PYTHONPATH=ROOT, COLORTERM="truecolor")
        env.update(extra_env or {})
        p = subprocess.Popen([sys.executable, "-c", CHILD, result.name, mode], stdin=slave, stdout=slave,
                             stderr=subprocess.PIPE, env=env, start_new_session=True, cwd=ROOT)
        os.close(slave)
        buf = bytearray()
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                buf.extend(data)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            deadline = time.time() + 2
            while b"agentree" not in buf and time.time() < deadline:
                time.sleep(0.01)
            for k in keys:
                os.write(master, k)
                self.last_key_at = time.monotonic()
                time.sleep(0.06)
            if signal_after is not None:
                time.sleep(0.1)
                p.send_signal(signal_after)
            rc = p.wait(timeout=3)
            self.exited_at = time.monotonic()
        finally:
            if p.poll() is None:
                p.kill()
            stop.set()
            time.sleep(0.05)
            os.close(master)
        err = p.stderr.read().decode() if p.stderr else ""
        p.stderr.close()
        with open(result.name) as fh:
            data = json.load(fh) if os.path.getsize(result.name) else {}
        os.unlink(result.name)
        return rc, bytes(buf), data, err

    def test_session_flow(self):
        t0 = time.time()
        keys = [b"j", b" ", b"/", b"s", b"s", b"o", b"\x1b", b"W", b"P", b"?", b"\x1b", b"q"]
        rc, out, saved, err = self.run_child(keys)
        self.assertEqual(rc, 0, err)
        self.assertLess(time.time() - t0, 3.5)
        self.assertIn(b"\x1b[?1049h", out)
        self.assertIn(b"\x1b[?1049l", out)
        self.assertIn(b"\x1b[?25h", out)
        self.assertLess(out.rfind(b"\x1b[?1049h"), out.rfind(b"\x1b[?1049l"))
        self.assertIn(b"38;2;", out)                    # truecolor
        self.assertIn("Keys".encode(), out)             # help overlay was drawn
        self.assertEqual(saved["rc"], 0)
        # Esc in filter mode cleared the text; W moved to "with tab"; P turned panes on
        self.assertEqual(saved["ui"]["text"], "")
        self.assertEqual(saved["ui"]["mode"], "with_tab")
        self.assertTrue(saved["ui"]["panes"])
        self.assertIsNotNone(saved["first_frame_ms"])

    def test_256_colour_and_kitty_keys(self):
        # kitty CSI-u esc closes the help, ctrl+c (legacy) exits
        rc, out, saved, err = self.run_child([b"?", b"\x1b[27u", b"\x03"], cols=90, rows=24,
                                             extra_env={"COLORTERM": ""})
        self.assertEqual(rc, 0, err)
        self.assertIn(b"38;5;", out)
        self.assertNotIn(b"38;2;", out)
        self.assertIn(b"\x1b[?1049l", out)


    def test_sigterm_restores_terminal(self):
        import signal
        rc, out, saved, err = self.run_child([b"j"], signal_after=signal.SIGTERM)
        self.assertEqual(rc, 0, err)
        self.assertIn(b"\x1b[?1049l", out)
        self.assertIn(b"\x1b[?25h", out)
        self.assertIn("ui", saved)

    def test_quit_during_slow_refresh_exits_promptly(self):
        # the refresh is blocked in `sleep 5` (started via exec.run) when q arrives
        rc, out, saved, err = self.run_child([b"j", b"q"], mode="slow")
        self.assertEqual(rc, 0, err)
        self.assertLess(self.exited_at - self.last_key_at, 0.5)
        self.assertIn(b"\x1b[?1049l", out)
        self.assertEqual(len(saved["children"]), 1)     # the sleep was registered
        pid = saved["children"][0]
        deadline = time.time() + 1
        alive = True
        while alive and time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.02)
            except ProcessLookupError:
                alive = False
        self.assertFalse(alive, "sleep child survived agentree exit")

    def test_sigterm_during_action_waits_for_rollback(self):
        import signal
        rc, out, saved, err = self.run_child([b"x"], mode="exec", signal_after=signal.SIGTERM)
        self.assertEqual(rc, 0, err)
        self.assertTrue(saved.get("started"))
        self.assertTrue(saved.get("rolled_back"))       # cancel reached the action; it rolled back first
        self.assertLess(self.exited_at - self.last_key_at, 1.5)
        self.assertIn(b"\x1b[?1049l", out)
        self.assertIn(b"\x1b[?25h", out)
        self.assertNotIn("still running", err)

    def test_first_frame_hook_can_abort(self):
        rc, out, saved, err = self.run_child([], mode="bench")
        self.assertEqual(rc, 7, err)
        self.assertIn(b"\x1b[?1049l", out)
        self.assertIn(b"\x1b[?25h", out)
        self.assertLess(saved["first_frame_ms"], 30)


if __name__ == "__main__":
    unittest.main()
