"""The agentree popup: event loop, background refresh, key handling, terminal lifecycle.

`run_app` paints the given (maybe stale) snapshot right away, then refreshes in a
background thread (now, every ~2 s, and on R). Refresh results reach the main loop
through a queue plus a self-pipe, so `select()` wakes on either keys or new data.
"""
from __future__ import annotations

import os
import queue
import select
import signal
import sys
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from agentree.actions.api import ActionResult, Actions, AgentOptions, Plan, SpawnOptions
from agentree.config import Config
from agentree.model.tree import (
    cycle_worktree_mode,
    first_child_key,
    parent_key,
    select_move,
    session_key,
    set_fold,
    toggle,
    toggle_fold,
    row_for,
)
from agentree.types import Snapshot, UiState

from .compose import AppState, Status, compose, rebuild
from .screen import Canvas, Renderer
from .term import ESC_TIMEOUT, Key, KeyDecoder, Terminal, fileno
from .theme import Theme
from .widgets import AgentChooserModal, ConfirmModal, HelpModal, PromptModal
from .worker import ActionWorker, CancelledBeforeStart, Job

REFRESH_EVERY = 2.0
QUICK_GAP = 0.15                # live status: at most one agents-only refresh per this many seconds
STATUS_SECONDS = 3.0
TICK = 1.0                      # re-render ages at least this often
SPIN = 0.1                      # spinner frame time while an action runs
PLAN_DEBOUNCE = 0.08            # spawn preview: wait this long after the last edit
BUSY_MSG = "busy — wait"
QUIT_MSG = "action running — press q again to quit anyway"
STOP_MSG = "stopping… rolling back"
STOP_CAP = 10.0                 # forced quit: wait this long for the action to roll back
SIGNAL_CAP = 3.0                # SIGTERM/SIGHUP/EOF: shorter wait, no screen needed
STUCK_MSG = "agentree: action still running at exit; its child processes will be killed"
NO_AGENT_MSG = "No agent command found; run agentree doctor"
AGENT_ROWS = ("worktree", "agent", "pane")   # rows `o` (agent here) works on

TIMINGS: dict[str, float] = {}  # "first_frame_ms": run_app entry -> first flush


class _Terminated(BaseException):
    """Raised from SIGTERM/SIGHUP handlers to unwind and restore the terminal."""


class App:
    def __init__(self, *, cfg: Config, snapshot: Snapshot, ui: UiState, refresh: Callable[[], Snapshot],
                 actions: Actions, term: Terminal | None = None, theme: Theme | None = None,
                 home: str | None = None, size: tuple[int, int] = (120, 32), live: Any = None) -> None:
        self.cfg = cfg
        self.refresh_fn = refresh
        # live status (optional; see run_app): .start(poke), .quick(prev) -> Snapshot,
        # .interval() -> s until the next full refresh, .close()
        self.live = live
        self.quick_due: float | None = None
        self.next_quick: float | None = None   # periodic quick refresh (live.quick_interval())
        self.last_quick = 0.0
        self.actions = actions
        self.term = term
        th = theme or Theme(accent=getattr(cfg, "accent", None) or "#e5b454")
        st = AppState(snapshot=snapshot, ui=ui, view=None, theme=th, now=time.time(),  # type: ignore[arg-type]
                      home=home if home is not None else os.path.expanduser("~"))
        rebuild(st)
        self.st = st
        self.size = size
        self.renderer = Renderer(th.sgr)
        self.canvas: Canvas | None = None
        self.quit = False
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.wake_r = self.wake_w = -1
        # Guards wake_w against close-while-writing: refresh workers may outlive run_app.
        self._wake_lock = threading.Lock()
        self.refreshing = False
        self.refresh_again = False
        self.next_refresh = 0.0
        self.refresh_error = False
        self.resized = False
        self.last_tick = time.monotonic()
        self.worker = ActionWorker(self._post)
        self.quit_armed = False
        self.plan_due: float | None = None
        self.tick = 0
        self.exit_reason = "quit"       # "quit" | "eof" | "signal" | "error"
        self.stopping = False
        self.stop_deadline: float | None = None
        self.can_render = True

    def _post(self, msg: tuple) -> None:
        """Worker threads hand results to the main loop here."""
        self.q.put(msg)
        self.wake(b"a")

    # ------------------------------------------------------------------ status / render
    def set_status(self, text: str, tone: str = "info", seconds: float = STATUS_SECONDS) -> None:
        self.st.status = Status(text, tone, time.monotonic() + seconds if seconds else 0.0)

    def render(self) -> None:
        self.tick = int(time.monotonic() / SPIN)
        self.st.tick = self.tick
        job = self.worker.current
        if job is not None and not job.quiet:
            self.st.activity = Status(f"working… {job.what}" + (f" · {job.note}" if job.note else ""), "busy")
        else:
            self.st.activity = None
        if self.st.modal is not None and hasattr(self.st.modal, "tick"):
            self.st.modal.tick = self.tick
        if self.term is not None:
            self.size = self.term.size()
        cv = compose(self.st, self.size)
        self.st.scroll = cv.info.get("scroll", self.st.scroll)
        self.canvas = cv
        if self.term is not None:
            out = self.renderer.render(cv)
            if out:
                self.term.write(out)

    def _rebuild(self) -> None:
        rebuild(self.st, time.time())

    def _set_ui(self, ui: UiState) -> None:
        self.st.ui = ui
        self._rebuild()

    # ------------------------------------------------------------------ refresh
    def start_refresh(self, quick: bool = False) -> None:
        """Full refresh, or (`quick`) the live hook's agents-only refresh. Never two at once: a
        full one asked for meanwhile is queued; a quick one stays due until the current ends."""
        if self.stopping:
            return
        if self.refreshing:
            if quick:
                self.quick_due = self.quick_due or time.monotonic()
            else:
                self.refresh_again = True
            return
        self.refreshing = self.st.refreshing = True
        if quick:
            self.last_quick = time.monotonic()
            prev = self.st.snapshot
            fn = lambda: self.live.quick(prev)  # noqa: E731
        else:
            fn = self.refresh_fn

        def work() -> None:
            try:
                res = ("refresh", "ok", fn(), quick)
            except BaseException as e:  # noqa: BLE001 - reported in the status line
                res = ("refresh", "err", e, quick)
            self.q.put(res)
            self.wake(b"r")

        threading.Thread(target=work, name="agentree-refresh", daemon=True).start()

    def drain_results(self) -> None:
        while True:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                return
            if msg[0] == "refresh":
                self._on_refresh(msg[1], msg[2], quick=msg[3] if len(msg) > 3 else False)
            elif msg[0] == "poke":
                self._on_poke()
            elif msg[0] == "action":
                self._on_action(msg[1], msg[2])
            elif msg[0] == "progress":
                self._on_progress(msg[1], msg[2])

    def poke(self) -> None:
        """Live hook -> main loop, from any thread: agent status changed, refresh agents soon."""
        self._post(("poke",))

    def _on_poke(self) -> None:
        if self.live is not None and self.quick_due is None:
            self.quick_due = max(time.monotonic(), self.last_quick + QUICK_GAP)

    def _full_interval(self) -> float:
        if self.live is None:
            return REFRESH_EVERY
        try:
            return float(self.live.interval())
        except Exception:  # noqa: BLE001 - fall back to polling
            return REFRESH_EVERY

    def _quick_interval(self) -> float | None:
        fn = getattr(self.live, "quick_interval", None) if self.live is not None else None
        if fn is None:
            return None
        try:
            v = fn()
            return float(v) if v else None
        except Exception:  # noqa: BLE001 - optional
            return None

    def _on_refresh(self, kind: str, val, quick: bool = False) -> None:
        self.refreshing = self.st.refreshing = False
        if not quick:
            self.next_refresh = time.monotonic() + self._full_interval()
        qi = self._quick_interval()
        self.next_quick = None if qi is None else time.monotonic() + qi
        if kind == "ok" and isinstance(val, Snapshot):
            self.st.snapshot = val
            self.refresh_error = False
            self._rebuild()
        elif not self.refresh_error:
            self.refresh_error = True
            self.set_status(f"refresh failed: {val}", "error")
        if self.refresh_again:
            self.refresh_again = False
            self.start_refresh()

    # ------------------------------------------------------------------ actions (worker)
    @property
    def busy(self) -> bool:
        """A real (non-quiet) action is in flight."""
        job = self.worker.current
        return job is not None and not job.quiet

    def dispatch(self, job: Job) -> bool:
        """Run `job` on the action worker; refuse with a status message if one is in flight."""
        if not self.worker.submit(job):
            self.set_status(BUSY_MSG, "warn", 2.0)
            return False
        if not job.quiet:
            self.quit_armed = False
            self.st.status = None
        return True

    def _job(self, kind: str, what: str, fn, *args, quiet: bool = False, payload=None,
             progress: bool = False, cancel: threading.Event | bool = False) -> Job:
        """Every job gets a cancel event; `cancel` passes it to the action as `cancel=` (execute)."""
        job = Job(kind, what, fn, args, {}, quiet=quiet, payload=payload)
        if isinstance(cancel, threading.Event):
            job.cancel = cancel
        if progress:
            job.kwargs["progress"] = self.worker.progress_cb(job)
        if cancel is not False:
            job.kwargs["cancel"] = job.cancel
        return job

    def _on_progress(self, job: Job, ev) -> None:
        if job is not self.worker.current:
            return
        if job.kind == "spawn_exec":
            job.payload.on_progress(ev)
            return
        label = getattr(ev, "label", "")
        idx, total = getattr(ev, "index", 0), getattr(ev, "total", 0)
        job.note = (f"{idx + 1}/{total} " if total else "") + label

    def _on_action(self, job: Job, res) -> None:
        self.worker.finish(job)
        self.quit_armed = False
        if self.stopping:
            # exiting: only let the spawn dialog show how the rollback ended
            if job.kind == "spawn_exec":
                self._done_spawn_exec(job, res if isinstance(res, ActionResult)
                                      else ActionResult(False, f"spawn: {res}"))
            return
        if isinstance(res, CancelledBeforeStart):
            return
        if isinstance(res, BaseException):
            if job.kind == "plan_spawn":
                res = Plan(kind="spawn", confirm=None, errors=(f"preview failed: {res}",))
            else:
                res = ActionResult(False, f"{job.what}: {res}")
        getattr(self, "_done_" + job.kind)(job, res)
        self._maybe_plan(time.monotonic())

    def _result(self, res: ActionResult | None, what: str) -> None:
        if not isinstance(res, ActionResult):
            self.st.status = None
            return
        if res.close:
            self.quit = True
        if res.message:
            self.set_status(res.message, "ok" if res.ok else "error")
        elif not res.ok:
            self.set_status(f"{what} failed", "error")
        else:
            self.st.status = None
        if res.refresh:
            self.start_refresh()

    def _done_simple(self, job: Job, res) -> None:
        self._result(res, job.what)

    _done_execute = _done_rename = _done_simple

    def _done_plan_remove(self, job: Job, res) -> None:
        if not isinstance(res, Plan):
            self._result(res, "remove")
        elif res.errors:
            self.set_status(res.errors[0], "error")
        elif res.confirm is not None:
            self.st.modal = ConfirmModal.from_plan(res)
        else:
            self.execute(res, None)

    def run_action(self, name: str, what: str) -> None:
        row = self.st.selected_row
        if row is None:
            self.set_status("nothing selected", "error")
            return
        self.dispatch(self._job("simple", what, getattr(self.actions, name), row, self.st.snapshot))

    def remove(self) -> None:
        row = self.st.selected_row
        if row is None:
            self.set_status("nothing selected", "error")
            return
        self.dispatch(self._job("plan_remove", "remove", self.actions.plan_remove, row, self.st.snapshot))

    def execute(self, plan: Plan, typed: str | None) -> None:
        self.dispatch(self._job("execute", plan.kind, self.actions.execute, plan, typed, progress=True,
                                cancel=True))

    def rename(self) -> None:
        if self.worker.busy:
            self.set_status(BUSY_MSG, "warn", 2.0)
            return
        row = self.st.selected_row
        if row is None or not row.session:
            self.set_status("rename: select a session", "error")
            return
        target = row_for(self.st.view, session_key(row.session)) or row
        self.st.modal = PromptModal("Rename session", "name", row.session, "rename", payload=target)

    # ------------------------------------------------------------------ agent here (o)
    def agent_here(self) -> None:
        """Look up which agents can start (quiet job), then launch one in the row's place.
        The row and snapshot are captured now: moving the selection meanwhile changes nothing."""
        if self.worker.busy:
            self.set_status(BUSY_MSG, "warn", 2.0)
            return
        row = self.st.selected_row
        if row is None or row.kind not in AGENT_ROWS:
            self.set_status("agent here: select a worktree, agent or pane", "error")
            return
        self.dispatch(self._job("agent_options", "agent here", self.actions.agent_options, quiet=True,
                                payload=(row, self.st.snapshot)))

    def _done_agent_options(self, job: Job, res) -> None:
        row, snap = job.payload
        if not isinstance(res, AgentOptions):
            self._result(res if isinstance(res, ActionResult) else ActionResult(False, "agent here: no options"),
                         "agent here")
            return
        available = [c for c in res.choices if c.available]
        if not available:
            self.set_status(NO_AGENT_MSG, "error")
        elif len(available) == 1:
            self._launch_agent(row, snap, available[0].id)
        else:
            self.st.modal = AgentChooserModal(res.choices, res.default_provider, payload=(row, snap))

    def _launch_agent(self, row, snap: Snapshot, provider: str) -> None:
        self.dispatch(self._job("simple", "new agent", self.actions.new_agent, row, snap, provider))

    # ------------------------------------------------------------------ spawn
    def spawn(self) -> None:
        self.dispatch(self._job("spawn_options", "spawn", self.actions.spawn_options, self.st.selected_row,
                                self.st.snapshot, quiet=True))

    def _done_spawn_options(self, job: Job, res) -> None:
        if not isinstance(res, SpawnOptions):
            self._result(res if isinstance(res, ActionResult) else ActionResult(False, "spawn: no options"),
                         "spawn")
            return
        from .spawn import SpawnModal  # lazy: keeps startup light
        self.st.modal = SpawnModal(res, home=self.st.home)
        self.plan_due = time.monotonic()

    def _maybe_plan(self, now: float) -> None:
        """Debounced preview: ask plan_spawn for the current form values when the worker is idle."""
        m = self.st.modal
        if getattr(m, "kind", "") != "spawn" or m.phase != "edit":
            self.plan_due = None
            return
        if self.plan_due is None or now < self.plan_due or self.worker.busy:
            return
        self.plan_due = None
        req = m.request()
        if m.plan is not None and m.plan_req == req:
            if m.submit_pending and m.ready:
                self._spawn_run(m)
            return
        self.dispatch(self._job("plan_spawn", "preview", self.actions.plan_spawn, req, self.st.snapshot,
                                quiet=True, payload=(m, req)))

    def _done_plan_spawn(self, job: Job, res) -> None:
        m, req = job.payload
        if self.st.modal is not m or m.phase != "edit" or not isinstance(res, Plan):
            return
        m.set_plan(req, res)
        if m.stale:
            self.plan_due = self.plan_due or time.monotonic()
        elif m.submit_pending and m.ready:
            self._spawn_run(m)

    def _spawn_run(self, m) -> None:
        job = self._job("spawn_exec", "spawn", self.actions.execute, m.plan, None, progress=True,
                        payload=m, cancel=m.cancel)
        if self.dispatch(job):
            m.submit_pending = False
            m.start_run()

    def _done_spawn_exec(self, job: Job, res) -> None:
        m = job.payload
        if not isinstance(res, ActionResult):
            res = ActionResult(False, "spawn: no result")
        m.finish(res)
        if res.refresh:
            self.start_refresh()

    def _spawn_key(self, m, key: Key) -> None:
        res = m.handle(key)
        now = time.monotonic()
        if res == "changed":
            self.plan_due = now + PLAN_DEBOUNCE
        elif res == "cancel":
            self.st.modal = None
            self.plan_due = None
        elif res == "ok":
            if m.ready:
                self._spawn_run(m)
            else:
                m.submit_pending = True
                self.plan_due = min(self.plan_due or now, now)
        elif res == "close":
            self.st.modal = None
            r = m.result
            if r is not None and r.close:
                self.quit = True
        elif res == "quit":
            self.request_quit("q")
        self._maybe_plan(now)

    # ------------------------------------------------------------------ quit
    def request_quit(self, key: str) -> None:
        """Quit, unless a real action is running: then the first q warns, the second quits."""
        if not self.busy:
            self.quit = True
            return
        if self.quit_armed and key in ("q", "ctrl+c"):
            self.quit = True
            return
        self.quit_armed = True
        self.set_status(QUIT_MSG, "warn", 4.0)

    # ------------------------------------------------------------------ keys
    def handle(self, key: Key) -> None:
        n = key.name
        if n == "ctrl+c":
            self.request_quit("ctrl+c")
            return
        if self.st.modal is not None:
            self._modal_key(key)
        elif self.st.mode == "filter":
            self._filter_key(key)
        else:
            self._normal_key(key)

    def _modal_key(self, key: Key) -> None:
        m = self.st.modal
        if key.name in ("click", "wheelup", "wheeldown"):
            return
        if getattr(m, "kind", "") == "spawn":
            self._spawn_key(m, key)
            return
        if isinstance(m, AgentChooserModal):
            res = m.handle(key)
            if res is None:
                return
            self.st.modal = None
            if res == "ok" and m.provider is not None:
                row, snap = m.payload
                self._launch_agent(row, snap, m.provider)
            return
        res = m.handle(key)
        if res is None:
            return
        self.st.modal = None
        if res != "ok":
            return
        if isinstance(m, ConfirmModal) and isinstance(m.payload, Plan):
            self.execute(m.payload, m.typed)
        elif isinstance(m, PromptModal):
            row = m.payload
            new = m.value.strip()
            if row is not None and new and new != row.session:
                self.dispatch(self._job("rename", "rename", self.actions.rename_session, row,
                                        self.st.snapshot, new))

    def _filter_key(self, key: Key) -> None:
        n = key.name
        ui = self.st.ui
        if n == "esc":
            self.st.mode = "normal"
            self._set_ui(replace(ui, text=""))
        elif n in ("enter", "down", "ctrl+enter"):
            self.st.mode = "normal"
        elif n == "backspace":
            self._set_ui(replace(ui, text=ui.text[:-1]))
        elif n == "ctrl+u":
            self._set_ui(replace(ui, text=""))
        elif n == "ctrl+w":
            t = ui.text.rstrip()
            self._set_ui(replace(ui, text=t[:t.rfind(" ") + 1]))
        elif n == "paste":
            self._set_ui(replace(ui, text=ui.text + " ".join(key.text.splitlines())))
        elif len(n) == 1 and key.text:
            self._set_ui(replace(ui, text=ui.text + n))

    def _move(self, delta: int) -> None:
        sel = select_move(self.st.view, self.st.ui.selected, delta)
        if sel is not None:
            self.st.ui = replace(self.st.ui, selected=sel)

    def _page(self) -> int:
        rows = self.canvas.info.get("tree_rows", 10) if self.canvas else 10
        return max(1, rows - 1)

    def _normal_key(self, key: Key) -> None:
        n = key.name
        st = self.st
        ui = st.ui
        row = st.selected_row
        if n in ("j", "down"):
            self._move(1)
        elif n in ("k", "up"):
            self._move(-1)
        elif n == "pgdn":
            self._move(self._page())
        elif n == "pgup":
            self._move(-self._page())
        elif n == "home":
            self._move(-10**9)
        elif n == "end":
            self._move(10**9)
        elif n in ("h", "left"):
            if row is not None and row.caret == "open":
                self._set_ui(set_fold(ui, row.key, False))
            else:
                p = parent_key(st.view, ui.selected)
                if p:
                    st.ui = replace(ui, selected=p)
        elif n in ("l", "right"):
            if row is not None and row.caret == "closed":
                self._set_ui(set_fold(ui, row.key, True))
            elif row is not None and row.caret == "open":
                c = first_child_key(st.view, row.key)
                if c:
                    st.ui = replace(ui, selected=c)
        elif n == " ":
            if row is not None:
                self._set_ui(toggle_fold(ui, st.view, row.key))
        elif n == "enter":
            self.run_action("jump", "jump")
        elif n == "/":
            st.mode = "filter"
        elif n == "esc":
            if ui.text:
                self._set_ui(replace(ui, text=""))
            else:
                self.request_quit("esc")
        elif n == "q":
            self.request_quit("q")
        elif n == "W":
            self._set_ui(cycle_worktree_mode(ui))
        elif n == "A":
            self._set_ui(toggle(ui, "show_agents"))
        elif n == "P":
            self._set_ui(toggle(ui, "show_panes"))
        elif n == "D":
            self._set_ui(toggle(ui, "show_detached"))
        elif n == "o":
            self.agent_here()
        elif n == "t":
            self.run_action("open_tab", "open tab")
        elif n == "g":
            self.run_action("lazygit", "lazygit")
        elif n == "x":
            self.remove()
        elif n == "r":
            self.rename()
        elif n == "ctrl+n":
            self.spawn()
        elif n == "R":
            self.start_refresh()
            self.set_status("refreshing…", "info", 1.0)
        elif n == "?":
            st.modal = HelpModal()
        elif n in ("wheelup", "wheeldown"):
            self._move(-3 if n == "wheelup" else 3)
        elif n == "click":
            self._click(key)

    def _click(self, key: Key) -> None:
        if self.canvas is None:
            return
        for y, x0, x1, kind, hit in self.canvas.hits:
            if y != key.y or not x0 <= key.x < x1:
                continue
            if kind == "row":
                if hit == self.st.ui.selected:
                    self._set_ui(toggle_fold(self.st.ui, self.st.view, hit))
                else:
                    self.st.ui = replace(self.st.ui, selected=hit)
            elif kind == "filter":
                self.st.mode = "filter"
            elif kind == "chip":
                what, _, arg = hit.partition(":")
                if what == "mode":
                    self._set_ui(replace(self.st.ui, worktree_mode=arg))
                else:
                    self._set_ui(toggle(self.st.ui, arg))
            return

    # ------------------------------------------------------------------ loop
    def _timeout(self, decoder: KeyDecoder) -> float:
        if decoder.pending:
            return ESC_TIMEOUT
        now = time.monotonic()
        t = TICK - (now - self.last_tick)
        if not self.refreshing:
            t = min(t, self.next_refresh - now)
            if self.quick_due is not None:
                t = min(t, self.quick_due - now)
            if self.next_quick is not None:
                t = min(t, self.next_quick - now)
        if self.st.status is not None and self.st.status.until:
            t = min(t, self.st.status.until - now)
        if self.plan_due is not None:
            t = min(t, self.plan_due - now)
        if self.busy:
            t = min(t, SPIN)
        return max(0.0, min(t, TICK))

    def step_timers(self, now: float) -> None:
        """Time-driven work between events: status expiry, refresh cadence, preview debounce."""
        if self.st.status is not None and self.st.status.until and now >= self.st.status.until:
            self.st.status = None
        if not self.refreshing and now >= self.next_refresh:
            self.start_refresh()
        if not self.refreshing and self.next_quick is not None and now >= self.next_quick:
            self.next_quick = None
            if self.quick_due is None:
                self.quick_due = now
        if not self.refreshing and self.quick_due is not None and now >= self.quick_due:
            self.quick_due = None
            self.start_refresh(quick=True)
        self._maybe_plan(now)
        if now - self.last_tick >= TICK:
            self.last_tick = now
            self._rebuild()

    def loop(self) -> None:
        assert self.term is not None
        in_fd = self.term.in_fd
        decoder = KeyDecoder()
        while not self.quit:
            try:
                ready = select.select([in_fd, self.wake_r], [], [], self._timeout(decoder))[0]
            except InterruptedError:
                ready = []
            keys: list[Key] = []
            if in_fd in ready:
                try:
                    data = os.read(in_fd, 4096)
                except (BlockingIOError, InterruptedError):
                    data = None
                except OSError:
                    data = b""                 # EIO after a hangup: same as EOF
                if data == b"":
                    self.exit_reason = "eof"   # terminal went away
                    self.can_render = False
                    return
                if data:
                    keys = decoder.feed(data)
            elif decoder.pending and not ready:
                keys = decoder.flush()
            if self.wake_r in ready:
                try:
                    os.read(self.wake_r, 512)
                except OSError:
                    pass
            self.drain_results()
            if self.resized:
                self.resized = False
                self.renderer.invalidate()
            for k in keys:
                self.handle(k)
                if self.quit:
                    return
            self.step_timers(time.monotonic())
            self.render()

    # ------------------------------------------------------------------ shutdown
    def stop_actions(self, cap: float, render: bool) -> bool:
        """Cancel the in-flight action and wait (up to `cap` s) for it to finish, then join the worker.

        Cooperative: with nothing running this returns at once. With an action running the
        screen stays up (if `render`) showing "stopping… rolling back" plus the action's own
        progress events, until its result arrives or the cap runs out. Returns False when the
        action was still running at the deadline (emergency path).
        """
        self.stopping = True
        self.plan_due = None
        job = self.worker.close()
        now = time.monotonic()
        self.stop_deadline = now + max(0.0, cap)
        render = render and self.can_render and self.term is not None
        if job is not None and not job.quiet:
            m = self.st.modal
            if getattr(m, "kind", "") == "spawn" and m.phase == "running":
                m.footer = ("cancelling… rolling back", "accent")
        in_fd = self.term.in_fd if (self.term is not None and render) else -1
        while self.worker.current is not None:
            now = time.monotonic()
            left = self.stop_deadline - now
            if left <= 0:
                break
            cur = self.worker.current
            if not cur.quiet:
                note = f" · {cur.note}" if cur.note else ""
                self.st.status = Status(STOP_MSG + note, "busy", 0)
            if render:
                try:
                    self.render()
                except OSError:
                    render, in_fd = False, -1
            fds = [fd for fd in (self.wake_r, in_fd) if fd >= 0]
            ready: list[int] = []
            if fds:
                try:
                    ready = select.select(fds, [], [], min(SPIN, left))[0]
                except (InterruptedError, OSError, ValueError):
                    ready = []
            else:
                time.sleep(min(0.02, left))
            for fd in ready:
                try:
                    data = os.read(fd, 4096)       # stdin keys are dropped while stopping
                except OSError:
                    data = b"x"
                if fd == in_fd and not data:
                    in_fd = -1                     # EOF: stop watching the terminal
            self.drain_results()
        if render:
            try:
                self.render()
            except OSError:
                pass
        left = max(0.0, self.stop_deadline - time.monotonic())
        # the result is in: the thread is only finishing its loop, so the join is instant
        clean = self.worker.shutdown(min(left, 1.0) if self.worker.current is None else 0.0)
        return clean and self.worker.current is None

    def on_signal_while_stopping(self, *_: object) -> None:
        """SIGTERM/SIGHUP during a stop: cap the wait at SIGNAL_CAP; a second one stops waiting."""
        now = time.monotonic()
        if self.exit_reason == "signal":
            self.stop_deadline = now
        else:
            self.exit_reason = "signal"
            if self.stop_deadline is None or self.stop_deadline > now + SIGNAL_CAP:
                self.stop_deadline = now + SIGNAL_CAP
        self.wake(b"s", blocking=False)

    def on_winch(self, *_: object) -> None:
        self.resized = True
        # signal handler (main thread): never block on the lock the main thread may hold
        self.wake(b"w", blocking=False)

    def wake(self, b: bytes, blocking: bool = True) -> None:
        """Poke the main loop's select(); a no-op once the wake pipe is closed."""
        if not self._wake_lock.acquire(blocking):
            return
        try:
            if self.wake_w >= 0:
                try:
                    os.write(self.wake_w, b)
                except OSError:
                    pass
        finally:
            self._wake_lock.release()

    def close_wake_pipe(self) -> None:
        """Close both wake fds, whatever the refresh state; late writers see wake_w == -1."""
        with self._wake_lock:
            fds = (self.wake_r, self.wake_w)
            self.wake_r = self.wake_w = -1
        for fd in fds:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _raise_terminated(signum, frame):  # noqa: ARG001
    raise _Terminated(signum)


def run_app(*, cfg: Config, snapshot: Snapshot, ui: UiState, refresh: Callable[[], Snapshot],
            actions: Actions, save_ui: Callable[[UiState], None],
            on_first_frame: Callable[[], None] | None = None,
            tty_in=None, tty_out=None, live: Any = None) -> int:
    """Run the popup until the user closes it. Always restores the terminal. Returns 0.

    `live` (optional) drives live agent status: after the first frame `live.start(poke)` is
    called with a thread-safe `poke()` (-> an agents-only `live.quick(snapshot)` refresh, at most
    every QUICK_GAP s, never overlapping another refresh); `live.interval()` gives the delay to
    the next full refresh; `live.quick_interval()` (optional; None = off) the delay to the next
    periodic agents-only refresh; `live.close()` runs on exit (it also stops the status
    watchers)."""
    t0 = time.perf_counter()
    in_fd = fileno(tty_in, sys.stdin)
    out_fd = fileno(tty_out, sys.stdout)
    term = Terminal(in_fd, out_fd, mouse=os.environ.get("AGENTREE_MOUSE", "1") != "0")
    app = App(cfg=cfg, snapshot=snapshot, ui=ui, refresh=refresh, actions=actions, term=term, live=live)
    app.wake_r, app.wake_w = os.pipe()
    os.set_blocking(app.wake_r, False)
    os.set_blocking(app.wake_w, False)
    old: dict[int, object] = {}
    try:
        try:
            for sig, fn in ((signal.SIGWINCH, app.on_winch), (signal.SIGTERM, _raise_terminated),
                            (signal.SIGHUP, _raise_terminated)):
                old[sig] = signal.signal(sig, fn)
        except ValueError:
            pass  # not the main thread: no signal handlers, still works
        term.enter()
        app.render()
        TIMINGS["first_frame_ms"] = (time.perf_counter() - t0) * 1000
        if on_first_frame is not None:
            on_first_frame()
        if live is not None:
            try:
                live.start(app.poke)
            except Exception as e:  # noqa: BLE001 - live status is optional: keep polling
                app.live = None
                app.set_status(f"live status unavailable: {e}", "error")
        app.start_refresh()
        app.loop()
        return 0
    except _Terminated:
        app.exit_reason = "signal"
        return 0
    except BaseException:
        if app.exit_reason == "quit":
            app.exit_reason = "error"
        raise
    finally:
        clean = _stop(app, old)
        if live is not None:
            try:
                live.close()               # before the CLI kills children; prompt (sockets shut)
            except Exception:  # noqa: BLE001
                pass
        term.leave()
        for sig, h in old.items():
            try:
                signal.signal(sig, h)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                pass
        app.close_wake_pipe()
        try:
            save_ui(app.st.ui)
        except Exception:  # noqa: BLE001 - never fail the exit on a state write
            pass
        if not clean:
            try:
                sys.stderr.write(STUCK_MSG + "\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass


def _stop(app: App, handlers: dict) -> bool:
    """Exit path for every way out of run_app: stop the action worker before returning."""
    try:
        if handlers:                       # we own SIGTERM/SIGHUP: make them cap the wait instead
            for sig in (signal.SIGTERM, signal.SIGHUP):
                try:
                    signal.signal(sig, app.on_signal_while_stopping)
                except (ValueError, TypeError):
                    pass
        cooperative = app.exit_reason == "quit"
        return app.stop_actions(STOP_CAP if cooperative else SIGNAL_CAP, render=cooperative)
    except BaseException:  # noqa: BLE001 - the terminal must be restored whatever happens
        return not app.worker.alive
