"""The single action worker thread.

Every Actions call runs here, never on the terminal loop. At most one job is in flight.
Results and progress events are handed to `post` (the app's queue + wake pipe) and
applied on the main loop; job code never touches UI state.

`current` is read and cleared on the main loop only (`finish`), so there is no race
between "is it busy?" and "start another one".

Stopping
--------
Every job carries a `threading.Event` (`job.cancel`). `execute` jobs receive it as
`execute(..., cancel=event)`; other actions never see it (it is simply set).

* Cooperative cancellation (the normal way out): `close()` / `shutdown()` set the running
  job's cancel event. A cancellable action notices it at its next step boundary, rolls back
  what it created (reporting that through its progress callback), and returns. The app keeps
  draining those progress events, so the user sees the rollback, and waits for the result.
  `shutdown()` then posts a stop sentinel and joins the thread.

* Emergency termination (the cap ran out): if the job does not return in time,
  `shutdown()` returns False and leaves the daemon thread behind. The app says so on stderr
  and returns; the cli then kills the child processes the action started. Nothing here
  kills threads or processes itself.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

_STOP = object()          # sentinel: the worker thread exits when it dequeues this


class CancelledBeforeStart(Exception):
    """Posted instead of running a job that was cancelled while still queued."""


@dataclass(eq=False)
class Job:
    kind: str                         # "simple" | "plan_remove" | "execute" | "rename" | "spawn_options" | ...
    what: str                         # short words for the status line, e.g. "jump"
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    quiet: bool = False               # cheap & side-effect free: no spinner, no quit warning
    payload: Any = None               # whatever the main loop needs to apply the result
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float = 0.0
    note: str = ""                    # latest progress text for the status line


class ActionWorker:
    def __init__(self, post: Callable[[tuple], None]) -> None:
        self.post = post
        self.current: Job | None = None
        self.closed = False
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stop_sent = False

    @property
    def busy(self) -> bool:
        return self.current is not None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, job: Job) -> bool:
        """Queue `job` if nothing is in flight. Returns False (and does nothing) when busy or closed."""
        if self.current is not None or self.closed:
            return False
        job.started = time.monotonic()
        self.current = job
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="agentree-action", daemon=True)
            self._thread.start()
        self._jobs.put(job)
        return True

    def finish(self, job: Job) -> None:
        """Main loop: the result of `job` has been applied."""
        if self.current is job:
            self.current = None

    def progress_cb(self, job: Job) -> Callable[[Any], None]:
        return lambda ev: self.post(("progress", job, ev))

    def close(self) -> Job | None:
        """Stop accepting jobs and ask the in-flight one to cancel. Returns that job, if any."""
        self.closed = True
        job = self.current
        if job is not None:
            job.cancel.set()
        return job

    def shutdown(self, timeout: float) -> bool:
        """close(), then stop the thread and join it for up to `timeout` seconds.

        True when the thread has exited (or never started); False when a job is still
        running after `timeout` (emergency path: the caller should warn and move on).
        """
        self.close()
        t = self._thread
        if t is None:
            return True
        if not self._stop_sent:
            self._stop_sent = True
            self._jobs.put(_STOP)
        t.join(max(0.0, timeout))
        if t.is_alive():
            return False
        self._thread = None
        return True

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is _STOP:
                return
            if self.closed and job.cancel.is_set():
                self.post(("action", job, CancelledBeforeStart(f"{job.what}: cancelled")))
                continue
            try:
                res: Any = job.fn(*job.args, **job.kwargs)
            except BaseException as e:  # noqa: BLE001 - reported on the main loop
                res = e
            self.post(("action", job, res))
