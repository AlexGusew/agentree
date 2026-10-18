"""Deadline-bounded parallel map on daemon threads.

Sources must never use concurrent.futures: ThreadPoolExecutor workers are joined at interpreter
shutdown (even when created from a daemon thread), so one hung subprocess or socket would block
agentree's exit. Threads started here are daemons and are never joined; abandoned work is bounded by
the per-call timeouts of exec.run / the HTTP client, and exec.shutdown() kills its children.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence

OK = "ok"
ERR = "err"
PENDING = "pending"     # not finished (or not started) by the deadline


def run_all(fns: Sequence[Callable[[], Any]], *, workers: int | None = None,
            deadline: float | None = None, name: str = "agentree-par") -> list[tuple[str, Any]]:
    """Run every zero-arg callable, at most `workers` at a time, on daemon threads.

    Waits until all are done or the monotonic `deadline` passes (None: wait for all; each fn
    must then bound itself). Returns [(OK, value) | (ERR, exception) | (PENDING, None)] aligned
    with `fns`. Items not yet started when the deadline passes are skipped.
    """
    n = len(fns)
    results: list[tuple[str, Any]] = [(PENDING, None)] * n
    if n == 0:
        return results
    cond = threading.Condition()
    state = {"next": 0, "left": n}

    def worker() -> None:
        while True:
            with cond:
                i = state["next"]
                if i >= n:
                    return
                state["next"] = i + 1
            if deadline is not None and time.monotonic() >= deadline:
                r: tuple[str, Any] = (PENDING, None)
            else:
                try:
                    r = (OK, fns[i]())
                except BaseException as e:  # noqa: BLE001 - reported to the caller
                    r = (ERR, e)
            with cond:
                results[i] = r
                state["left"] -= 1
                cond.notify_all()

    for _ in range(min(n, workers or n)):
        threading.Thread(target=worker, name=name, daemon=True).start()
    with cond:
        while state["left"] > 0:
            if deadline is None:
                cond.wait()
                continue
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            cond.wait(rem)
        return list(results)


def value(outcome: tuple[str, Any], default: Any = None) -> Any:
    return outcome[1] if outcome[0] == OK else default
