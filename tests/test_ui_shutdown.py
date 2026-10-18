"""Action worker shutdown: cooperative cancel + rollback, emergency cap, no leaked threads."""
import contextlib
import io
import os
import signal
import threading
import time
import unittest

from agentree.actions.api import ActionResult, NullActions, Plan, ProgressEvent
from agentree.config import Config
from agentree.model.sample import sample_snapshot
from agentree.types import UiState
from agentree.ui import app as appmod
from agentree.ui.app import App
from agentree.ui.term import Key
from agentree.ui.worker import ActionWorker, Job

NOW = 1_800_000_000.0
SSO = "w:web-app:/Users/me/Projects/web-app/.worktrees/sso-login"


def action_threads():
    return [t for t in threading.enumerate() if t.name == "agentree-action" and t.is_alive()]


def join_stragglers(timeout=2.0):
    """Stuck-action tests leave a thread behind on purpose; once released, let it finish."""
    for t in action_threads():
        t.join(timeout)


class Removable(NullActions):
    """x -> a plan without confirm -> execute.

    cooperative=True: waits for cancel, then reports a rollback and returns.
    cooperative=False: ignores cancel and blocks until `release` is set.
    """

    def __init__(self, cooperative=True):
        super().__init__()
        self.cooperative = cooperative
        self.release = threading.Event()
        self.started = threading.Event()
        self.cancel_seen = False
        self.returned = threading.Event()

    def plan_remove(self, row, snap):
        return Plan("remove-worktree", None, (("git", "worktree", "remove", "x"),))

    def jump(self, row, snap):
        return ActionResult(True, "jumped")

    def execute(self, plan, typed=None, *, progress=None, cancel=None):
        try:
            progress(ProgressEvent(0, 2, "close tab", "ok"))
            progress(ProgressEvent(1, 2, "git worktree remove", "running"))
            self.started.set()
            if not self.cooperative:
                self.release.wait(10)
                return ActionResult(True, "removed")
            if cancel.wait(10):
                self.cancel_seen = True
                time.sleep(0.15)                       # rollback takes a moment
                progress(ProgressEvent(1, 2, "git worktree remove", "failed", "cancelled"))
                progress(ProgressEvent(0, 2, "reopen tab", "rolled_back", "tab reopened"))
                return ActionResult(False, "cancelled, rolled back")
            return ActionResult(True, "removed")
        finally:
            self.returned.set()


def make_app(actions):
    snap = sample_snapshot(NOW)
    return App(cfg=Config(), snapshot=snap, ui=UiState(selected=SSO), refresh=lambda: snap,
               actions=actions, home="/Users/me", size=(120, 32))


def pump_until(app, pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.drain_results()
        if pred():
            return True
        time.sleep(0.005)
    return False


class WorkerShutdown(unittest.TestCase):
    def test_idle_worker_joins_thread(self):
        before = len(action_threads())
        posted = []
        w = ActionWorker(posted.append)
        self.assertTrue(w.shutdown(0.5))                 # never started: instant
        w = ActionWorker(posted.append)
        job = Job("simple", "x", lambda: 42)
        self.assertTrue(w.submit(job))
        deadline = time.monotonic() + 1
        while not posted and time.monotonic() < deadline:
            time.sleep(0.005)
        w.finish(job)
        self.assertEqual(len(action_threads()), before + 1)
        t0 = time.monotonic()
        self.assertTrue(w.shutdown(1.0))
        self.assertLess(time.monotonic() - t0, 0.5)       # cooperative path is instant
        self.assertEqual(len(action_threads()), before)
        self.assertFalse(w.submit(Job("simple", "y", lambda: 1)))   # closed

    def test_every_job_has_a_cancel_event_set_on_close(self):
        w = ActionWorker(lambda m: None)
        gate = threading.Event()
        job = Job("simple", "jump", lambda: gate.wait(2))
        self.assertIsInstance(job.cancel, threading.Event)
        w.submit(job)
        self.assertIs(w.close(), job)
        self.assertTrue(job.cancel.is_set())
        gate.set()
        self.assertTrue(w.shutdown(1.0))

    def test_execute_gets_the_jobs_cancel_event(self):
        acts = Removable()
        app = make_app(acts)
        app.handle(Key("x", "x"))
        self.assertTrue(pump_until(app, lambda: acts.started.is_set()))
        job = app.worker.current
        self.assertIs(job.kwargs["cancel"], job.cancel)
        job.cancel.set()
        self.assertTrue(pump_until(app, lambda: app.worker.current is None))
        self.assertTrue(app.worker.shutdown(1.0))


class ForcedQuit(unittest.TestCase):
    def test_forced_quit_waits_for_rollback(self):
        acts = Removable(cooperative=True)
        app = make_app(acts)
        seen = []
        orig = app._on_progress
        app._on_progress = lambda job, ev: (seen.append((ev.label, ev.state)), orig(job, ev))
        app.handle(Key("x", "x"))
        self.assertTrue(pump_until(app, lambda: acts.started.is_set()))
        app.handle(Key("q", "q"))
        self.assertFalse(app.quit)                       # first q only warns
        app.handle(Key("q", "q"))
        self.assertTrue(app.quit)
        t0 = time.monotonic()
        self.assertTrue(app.stop_actions(10.0, render=False))
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(acts.cancel_seen)
        self.assertIn(("reopen tab", "rolled_back"), seen)  # rollback progress drained before exit
        self.assertTrue(app.st.status.text.startswith("stopping… rolling back"))
        self.assertEqual(action_threads().count(app.worker._thread), 0)
        self.assertIsNone(app.worker._thread)

    def test_non_cooperative_job_hits_the_cap(self):
        acts = Removable(cooperative=False)
        app = make_app(acts)
        app.handle(Key("x", "x"))
        self.assertTrue(pump_until(app, lambda: acts.started.is_set()))
        t0 = time.monotonic()
        self.assertFalse(app.stop_actions(0.3, render=False))
        took = time.monotonic() - t0
        self.assertGreaterEqual(took, 0.3)
        self.assertLess(took, 1.5)
        acts.release.set()                               # let the stuck thread end
        self.assertTrue(acts.returned.wait(2))
        self.assertTrue(app.worker.shutdown(2.0))

    def test_stop_with_nothing_running_is_instant(self):
        app = make_app(Removable())
        app.handle(Key("enter"))                         # jump: quick action
        self.assertTrue(pump_until(app, lambda: app.worker.current is None))
        t0 = time.monotonic()
        self.assertTrue(app.stop_actions(10.0, render=False))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertIsNone(app.worker._thread)


class RunAppExit(unittest.TestCase):
    """run_app in-process on a real pty."""

    def hang_up(self):
        os.close(self.master)                            # the terminal goes away

    def run_pty(self, actions, script, caps=None):
        master, slave = os.openpty()
        self.master = master
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    if not os.read(master, 65536):
                        return
                except OSError:
                    return

        def feed():
            time.sleep(0.15)
            for item in script:
                if isinstance(item, float):
                    time.sleep(item)
                elif callable(item):
                    item()
                else:
                    os.write(master, item)
                    time.sleep(0.05)

        saved = {k: getattr(appmod, k) for k in (caps or {})}
        for k, v in (caps or {}).items():
            setattr(appmod, k, v)
        threading.Thread(target=drain, daemon=True).start()
        threading.Thread(target=feed, daemon=True).start()
        err = io.StringIO()
        t0 = time.monotonic()
        try:
            with contextlib.redirect_stderr(err):
                rc = appmod.run_app(cfg=Config(), snapshot=sample_snapshot(NOW), ui=UiState(selected=SSO),
                                    refresh=lambda: sample_snapshot(NOW), actions=actions,
                                    save_ui=lambda ui: None, tty_in=slave, tty_out=slave)
        finally:
            for k, v in saved.items():
                setattr(appmod, k, v)
            stop.set()
            for fd in (slave, master):
                try:
                    os.close(fd)
                except OSError:
                    pass
        return rc, time.monotonic() - t0, err.getvalue()

    def test_forced_quit_rolls_back_then_returns(self):
        acts = Removable(cooperative=True)
        rc, took, err = self.run_pty(acts, [b"x", 0.2, b"q", b"q"])
        self.assertEqual(rc, 0)
        self.assertTrue(acts.cancel_seen)
        self.assertTrue(acts.returned.is_set())          # the action finished before run_app returned
        self.assertNotIn("still running", err)
        self.assertLess(took, 3)

    def test_forced_quit_stuck_action_warns(self):
        acts = Removable(cooperative=False)
        rc, took, err = self.run_pty(acts, [b"x", 0.2, b"q", b"q"], caps={"STOP_CAP": 0.4})
        acts.release.set()
        join_stragglers()
        self.assertEqual(rc, 0)
        self.assertIn("agentree: action still running at exit; its child processes will be killed", err)
        self.assertLess(took, 2.5)

    def test_sigterm_during_action_caps_wait(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signal handlers need the main thread")
        acts = Removable(cooperative=False)

        def term():
            # only once run_app owns SIGTERM, never kill the test runner
            if signal.getsignal(signal.SIGTERM) is appmod._raise_terminated:
                os.kill(os.getpid(), signal.SIGTERM)

        rc, took, err = self.run_pty(acts, [b"x", 0.2, term], caps={"SIGNAL_CAP": 0.4})
        acts.release.set()
        join_stragglers()
        self.assertEqual(rc, 0)
        self.assertIn("still running at exit", err)
        self.assertLess(took, 2.0)

    def test_eof_during_cooperative_action(self):
        acts = Removable(cooperative=True)
        rc, took, err = self.run_pty(acts, [b"x", 0.2, self.hang_up])
        self.assertEqual(rc, 0)
        self.assertTrue(acts.cancel_seen)                # terminal gone: cancel + wait for rollback
        self.assertTrue(acts.returned.is_set())
        self.assertNotIn("still running", err)
        self.assertLess(took, 2.0)

    def test_no_thread_leak_over_repeated_runs(self):
        join_stragglers()
        before = len(action_threads())
        for _ in range(4):
            acts = Removable()
            rc, _, _ = self.run_pty(acts, [b"\r", 0.1, b"q"])     # quick jump, then quit
            self.assertEqual(rc, 0)
        self.assertEqual(len(action_threads()), before)


if __name__ == "__main__":
    unittest.main()
