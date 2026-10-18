"""Jump: bring a session / tab / pane to the user (zellij tab -> pane -> kitty window).

Rules (workspace-picker findings + M2 gate, verified live on zellij 0.45.1 / kitty 0.48.2):
  * route to an explicit target: `go-to-tab-by-id`, then `focus-pane-id`, then focus the exact
    kitty window; never `switch-session` (it moves the *source* client);
  * session identity is (canonical socket dir, name). Before acting, sessions are listed fresh in
    every socket dir: a name live in several dirs is refused, a session that moved dirs or whose
    server restarted since the snapshot (younger than expected) is refused ("refresh"), a cached
    exited session that is live again is navigated to instead of being resurrected;
  * `zellij action` targets the session's "last active client": tab/pane navigation happens only
    with exactly one client, re-queried right before acting; a failed client query refuses;
  * targets are re-validated semantically against a fresh listing: a worktree's tab must still
    hold a pane inside the worktree (deepest root wins), an agent's pane must exist and the
    agent process must still carry that ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID;
  * `focus-pane-id` exit 2 with stderr exactly `Pane Terminal(N) is already focused` for the
    requested pane is success; `go-to-tab-by-id` silently ignores unknown ids;
  * `focus-pane-id` on a *tiled* pane exits 0 and changes nothing (0.45.1; it does focus floating
    panes), so the result is read back from `list-clients` (ZELLIJ_PANE_ID of the single client —
    the only reliable focus signal; list-panes `is_focused` is per layer and not exclusive). A
    tiled target not reached is approached with `focus-next-pane`, which walks the selectable
    tiled panes of the active tab in order, re-reading `list-clients` after each step, bounded by
    their count + 1, each step re-validated (client still in the tab, on a tiled pane).
    `focus-next-pane` never leaves the floating layer while it is shown and skips collapsed panes
    of a stack (only a stack's expanded pane is visited). `focus-pane-id` on a tiled pane while a
    shown floating layer has the focus hides that whole layer (verified live), and
    `toggle-floating-panes` hides it too, so the focus state is read BEFORE any focus mutation:
    a floating target gets a direct `focus-pane-id`; a tiled target in a tab whose shown layer
    holds any floating pane other than agentree's own (plugins included) gets neither focus-pane-id
    nor a toggle — it is reported; when agentree's own popup is the only one, a tracked
    `toggle-floating-panes` hands focus to the tiled layer (toggled back if focus then fails).
    All of it runs under one deadline (FOCUS_DEADLINE_S). A pane target not reached ends as
    ok=False, close=False, "tab and window reached; could not focus pane terminal_N (<reason>)"
    — never a loop;
  * kitty: only owned sockets (also for cached hosts); a host window is re-validated with a fresh
    `kitty @ ls` right before focusing it; `focus-window` selects the tab, raises the OS window
    and activates kitty (verified on macOS);
  * a live session without a client gets a new kitty tab running `zellij attach` (marked with the
    `agentree_session` user var); navigation waits until that client is listed.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping

from agentree.actions.api import ActionResult
from agentree.actions.runner import Runner, canonical_dir, default_socket_dir, xdg_socket_dir
from agentree.types import Agent, KittyHost, Row, SessionNode, Snapshot, WorktreeNode, ZPane, ZSession

ATTACH_WAIT_S = 3.0
POLL_S = 0.1
AGE_TOLERANCE_S = 5         # list-sessions ages have 1 s resolution; allow scheduling drift
FOCUS_DEADLINE_S = 3.0      # whole pane-focus read-back (focus-pane-id, toggle, walk)
# ToggleFloatingPanes in ~/.config/zellij/config.kdl: `Ctrl Alt f` (normal/locked), `w` in pane mode
HIDE_FLOATING_KEYS = "Ctrl Alt f, or Alt p w"


# --------------------------------------------------------------------------- environment

@dataclass(frozen=True)
class Env:
    """The parts of the process environment the actions depend on."""
    current_session: str | None = None      # ZELLIJ_SESSION_NAME (session agentree runs in)
    socket_dir: str | None = None           # ZELLIJ_SOCKET_DIR (None: zellij default)
    kitty_listen_on: str | None = None      # KITTY_LISTEN_ON (kitty hosting agentree's window)
    uid: int = 0
    tmpdir: str = "/tmp"
    pane_id: str | None = None              # ZELLIJ_PANE_ID (agentree's own terminal pane)
    runtime_dir: str | None = None          # XDG_RUNTIME_DIR (Linux: zellij's default socket dir is under it)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "Env":
        e = os.environ if environ is None else environ
        return cls(e.get("ZELLIJ_SESSION_NAME") or None, e.get("ZELLIJ_SOCKET_DIR") or None,
                   e.get("KITTY_LISTEN_ON") or None, os.getuid(), e.get("TMPDIR") or "/tmp",
                   e.get("ZELLIJ_PANE_ID") or None, e.get("XDG_RUNTIME_DIR") or None)

    def _default_env(self) -> dict[str, str]:
        return {"TMPDIR": self.tmpdir, "XDG_RUNTIME_DIR": self.runtime_dir or ""}

    def current_dir(self) -> str:
        """Socket dir of the session agentree runs in (zellij default if ZELLIJ_SOCKET_DIR is unset)."""
        return self.socket_dir or default_socket_dir(self._default_env())

    def is_current(self, name: str, socket_dir: str | None) -> bool:
        return (self.current_session == name and socket_dir is not None
                and canonical_dir(self.current_dir()) == canonical_dir(socket_dir))

    def own_tab(self, name: str, socket_dir: str | None, panes) -> int | None:
        """Tab id holding agentree's own pane if `name`@`socket_dir` is the session agentree runs in.
        `panes`: ZPane records (snapshot or fresh listing)."""
        if not self.pane_id or not self.pane_id.isdigit() or not self.is_current(name, socket_dir):
            return None
        mine = int(self.pane_id)
        return next((p.tab_id for p in panes if not p.is_plugin and p.pane_id == mine), None)

    def exited_socket_dir(self) -> str:
        return self.socket_dir or xdg_socket_dir(self._default_env()) or f"/tmp/zellij-{self.uid}"


# --------------------------------------------------------------------------- worktree matching

def known_worktrees(snap: Snapshot) -> frozenset[str]:
    out: set[str] = set()
    for sn in snap.sessions:
        out.update(wn.worktree.path for wn in sn.worktrees)
    out.update(w.path for w in snap.orphans.worktrees)
    return frozenset(out)


def deepest(path: str | None, roots) -> str | None:
    """The deepest root containing `path` (findings: deepest worktree root wins)."""
    if not path:
        return None
    best = None
    for r in roots:
        if (path == r or path.startswith(r.rstrip("/") + "/")) and (best is None or len(r) > len(best)):
            best = r
    return best


# --------------------------------------------------------------------------- targets

@dataclass(frozen=True)
class Target:
    node: SessionNode
    tab_id: int | None = None       # None: session level (no tab navigation)
    pane: str | None = None         # zellij pane id ("terminal_3"); None: tab only
    label: str = ""                 # for messages
    worktree: str | None = None     # worktree jump: the tab must still hold a pane inside it
    roots: frozenset = frozenset()  # known worktree roots (deepest match)
    agent_pid: int | None = None    # agent jump: the process must still own the pane
    taken_at: float | None = None   # snapshot time (incarnation check)

    @property
    def navigates(self) -> bool:
        return self.tab_id is not None or self.pane is not None


def find_session(snap: Snapshot, name: str | None) -> SessionNode | None:
    if not name:
        return None
    for sn in snap.sessions:
        if sn.session.name == name:
            return sn
    return None


def _tab_of_pane(s: ZSession, pane_id: int) -> int | None:
    for t in s.tabs:
        for p in t.panes:
            if not p.is_plugin and p.pane_id == pane_id:
                return t.tab_id
    return None


def resolve(row: Row, snap: Snapshot) -> Target | str:
    """Jump target for a row, or a user-facing reason why there is none."""
    base = {"roots": known_worktrees(snap), "taken_at": snap.taken_at}
    if row.kind == "session" and isinstance(row.ref, SessionNode):
        return Target(row.ref, label=row.ref.session.name, **base)
    if row.kind == "worktree" and isinstance(row.ref, WorktreeNode):
        node = find_session(snap, row.session)
        if node is None:
            return "no session — press t to open a tab"
        wn = row.ref
        if node.session.state == "exited":
            return Target(node, label=node.session.name, **base)
        if not wn.tabs:
            return "no tab for this worktree — press t to open one"
        t = wn.tabs[0]
        return Target(node, tab_id=t.tab_id, label=f"{node.session.name} › {t.name}",
                      worktree=wn.worktree.path, **base)
    if row.kind == "agent" and isinstance(row.ref, Agent):
        a = row.ref
        node = find_session(snap, row.session or a.session_name)
        if node is None or a.pane_id is None:
            return "agent has no zellij pane (started outside zellij)"
        if node.session.state == "exited":
            return Target(node, label=node.session.name, **base)
        tab = _tab_of_pane(node.session, a.pane_id)
        if tab is None:
            return f"agent pane terminal_{a.pane_id} not found in {node.session.name}"
        return Target(node, tab_id=tab, pane=f"terminal_{a.pane_id}", label=f"{node.session.name} › agent",
                      agent_pid=a.pid, **base)
    if row.kind == "pane" and isinstance(row.ref, ZPane):
        node = find_session(snap, row.session)
        if node is None:
            return "pane has no session"
        p = row.ref
        return Target(node, tab_id=p.tab_id, pane=p.zid, label=f"{node.session.name} › {p.command or p.zid}",
                      **base)
    if row.kind == "group":
        return "no session — press t to open a tab"
    return "nothing to jump to"


# --------------------------------------------------------------------------- navigator

class _Fail(Exception):
    def __init__(self, message: str, close: bool = False) -> None:
        super().__init__(message)
        self.close = close


@dataclass(frozen=True)
class Fresh:
    live_dir: str | None            # socket dir where the session is live now (None: not live)
    age_s: int | None               # created age reported by list-sessions
    exited: bool = False            # listed as EXITED (resurrectable)


def _default_proc_env(pid: int) -> dict[str, str] | None:
    from agentree.sources import proc

    ae = proc.proc_args_env(pid)
    return ae[1] if ae else None


def _default_has_socket(socket_dir: str, name: str) -> bool:
    from agentree.sources import zellij as zsrc

    return zsrc.has_socket(socket_dir, name)


def _default_proc_start(pid: int) -> float | None:
    from agentree.actions.runner import proc_start_time

    return proc_start_time(pid)


def _default_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Navigator:
    runner: Runner
    env: Env
    zellij: str
    kitty: str
    sockets: Callable[[], list[str]]
    socket_dirs: Callable[[], list[str]] = lambda: []
    owned: Callable[[str], bool] = lambda s: True
    proc_env: Callable[[int], dict[str, str] | None] = _default_proc_env
    pid_alive: Callable[[int], bool] = _default_pid_alive
    proc_start: Callable[[int], float | None] = _default_proc_start
    has_socket: Callable[[str, str], bool] = _default_has_socket   # (socket dir, session) -> socket there
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    attach_wait_s: float = ATTACH_WAIT_S
    notes: list[str] = field(default_factory=list)
    renamed_own: str | None = None          # set by rename when agentree's own session was renamed
    focus_miss: tuple[str, str] | None = None   # (pane, reason): navigation reached the tab, not the pane
    focus_deadline_s: float = FOCUS_DEADLINE_S
    # called once zellij navigation is done, before kitty-only focus (`o`/`g` release their
    # session lock there); reset after the call
    on_zellij_done: Callable[[], None] | None = None

    # ---------------------------------------------------------------- command builders
    def zj(self, s: ZSession, *args: str) -> tuple[list[str], dict[str, str]]:
        env = {"ZELLIJ_SOCKET_DIR": s.socket_dir} if s.socket_dir else {}
        return [self.zellij, "--session", s.name, "action", *args], env

    def kitty_cmd(self, sock: str, *args: str) -> list[str]:
        return [self.kitty, "@", "--to", sock, *args]

    # ---------------------------------------------------------------- results
    def finish(self, ok: bool, message: str, close: bool = True) -> ActionResult:
        """Success after a pane target that could not be focused becomes ok=False, close=False with
        "tab and window reached; could not focus pane …", so the popup stays and shows it."""
        notes = "; ".join(self.notes)
        if ok and self.focus_miss is not None and not self.runner.dry_run:
            pane, why = self.focus_miss
            msg = f"tab and window reached; could not focus pane {pane} ({why})"
            return ActionResult(False, msg + (f" ({notes})" if notes else ""), close=False)
        if self.runner.dry_run and ok:
            n = len(self.runner.acted)
            msg = f"dry-run: {n} command{'s' if n != 1 else ''} logged"
            if notes:
                msg += f" ({notes})"
            return ActionResult(True, msg, close=False)
        if notes:
            message = f"{message} ({notes})" if message else notes
        return ActionResult(ok, message, close=close and ok)

    def fail(self, e: _Fail) -> ActionResult:
        return self.finish(False, str(e))

    # ---------------------------------------------------------------- session identity
    def lookup_session(self, name: str) -> Fresh | None:
        """List sessions (cheap) in every socket dir: None if `name` exists nowhere, Fresh(live)
        or Fresh(exited); refuses a name live in several dirs or an unlistable dir."""
        from agentree.sources import zellij as zsrc

        live: list[tuple[str, int | None]] = []
        exited = False
        seen: set[str] = set()
        for d in self.socket_dirs():
            cd = canonical_dir(d)
            if cd in seen:
                continue
            seen.add(cd)
            # without the caller's ZELLIJ* client env: inside session S, zellij 0.45.1 otherwise
            # reports S live "(current)" in every socket dir; a live listing without a socket
            # in that dir is downgraded to exited as well (zsrc.verify_live)
            r = self.runner.query(zsrc.list_sessions_argv(self.zellij), {"ZELLIJ_SOCKET_DIR": d})
            text = (r.stdout or "") + (r.stderr or "")
            if not r.ok and "No active zellij sessions" not in text:
                raise _Fail(f"cannot list zellij sessions in {d} — refusing")
            listed = [ps for ps in zsrc.parse_list_sessions(r.stdout or "") if ps.name == name]
            for ps in zsrc.verify_live({d: listed}, self.has_socket)[d]:
                if ps.exited:
                    exited = True
                else:
                    live.append((d, ps.created_age_s))
        if len(live) > 1:
            raise _Fail(f"session {name} is live in several socket dirs "
                        f"({', '.join(d for d, _ in live)}) — refusing")
        if live:
            return Fresh(live[0][0], live[0][1])
        if exited:
            return Fresh(None, None, exited=True)
        return None

    def fresh_session(self, name: str) -> Fresh:
        """lookup_session, refusing a session that no longer exists."""
        f = self.lookup_session(name)
        if f is None:
            raise _Fail(f"session {name} no longer exists — refresh (R)")
        return f

    def check_incarnation(self, s: ZSession, fresh: Fresh, taken_at: float | None) -> None:
        """Same socket dir and same server as in the snapshot, else refuse."""
        if s.socket_dir and canonical_dir(s.socket_dir) != canonical_dir(fresh.live_dir):
            raise _Fail(f"{s.name} now lives in {fresh.live_dir} — refresh (R)")
        if s.created_age_s is None or fresh.age_s is None or taken_at is None:
            return
        expected = s.created_age_s + max(0.0, self.clock() - taken_at)
        if fresh.age_s + AGE_TOLERANCE_S < expected:
            raise _Fail(f"{s.name}: session restarted — refresh (R)")

    # ---------------------------------------------------------------- zellij queries
    def fresh_inventory(self, s: ZSession):
        """(tabs, panes) of a live session from a fresh listing; raises _Fail if inconsistent."""
        from agentree.sources import zellij as zsrc

        for _attempt in (1, 2):
            rt = self.runner.query(*self.zj(s, "list-tabs", "--all", "--json"))
            rp = self.runner.query(*self.zj(s, "list-panes", "--tab", "--command", "--state", "--json"))
            inv = zsrc.parse_inventory(rt, rp)
            if not inv.errors and not inv.unknown_tabs:
                return inv.tabs, inv.panes
        why = "; ".join(inv.errors) or f"panes reference unknown tabs {inv.unknown_tabs}"
        raise _Fail(f"{s.name}: inventory incomplete ({why}) — refusing")

    def clients(self, s: ZSession) -> int:
        """Connected clients; a failed query is a refusal, never 'zero'."""
        from agentree.sources import zellij as zsrc

        r = self.runner.query(*self.zj(s, "list-clients"))
        if not r.ok:
            raise _Fail(f"{s.name}: list-clients failed ({(r.stderr or r.stdout).strip()[:120] or r.code}) — refusing")
        return zsrc.parse_list_clients(r.stdout)

    def wait_for_client(self, s: ZSession) -> int:
        """Poll until a client is attached; returns the actual count (dry-run: assumes 1)."""
        if self.runner.dry_run:
            return 1
        deadline = time.monotonic() + self.attach_wait_s
        while True:
            n = self.clients(s)
            if n >= 1:
                return n
            if time.monotonic() >= deadline:
                raise _Fail(f"{s.name}: no zellij client appeared within {self.attach_wait_s:.0f}s")
            self.sleep(POLL_S)

    # ---------------------------------------------------------------- navigation
    def validate(self, s: ZSession, t: Target) -> tuple[int | None, str | None]:
        """Re-check the target against a fresh listing; returns the (tab_id, pane) to act on."""
        tabs, panes = self.fresh_inventory(s)
        tab_id, pane = t.tab_id, t.pane
        if pane is not None:
            fresh = next((p for p in panes if p.zid == pane and not p.exited), None)
            if fresh is None:
                raise _Fail(f"{s.name}: pane {pane} is gone — refresh (R)")
            tab_id = fresh.tab_id          # panes can move between tabs
            if t.agent_pid is not None:
                self.validate_agent(s, t.agent_pid, fresh.pane_id)
        if tab_id is not None and not any(x.tab_id == tab_id for x in tabs):
            raise _Fail(f"{s.name}: tab {tab_id} is gone — refresh (R)")
        if t.worktree is not None and pane is None:
            roots = t.roots | {t.worktree}
            if not any(p.tab_id == tab_id and not p.is_plugin and not p.exited
                       and deepest(p.cwd, roots) == t.worktree for p in panes):
                raise _Fail(f"{s.name}: tab {tab_id} no longer holds {os.path.basename(t.worktree)} "
                            f"— stale, refresh (R)")
        return tab_id, pane

    def validate_agent(self, s: ZSession, pid: int, pane_id: int) -> None:
        if not self.pid_alive(pid):
            raise _Fail(f"agent {pid} has exited — refresh (R)")
        env = self.proc_env(pid)
        if env is None:
            raise _Fail(f"agent {pid}: cannot read its environment — refusing")
        if env.get("ZELLIJ_SESSION_NAME") != s.name or env.get("ZELLIJ_PANE_ID") != str(pane_id):
            raise _Fail(f"agent {pid} no longer runs in {s.name} pane terminal_{pane_id} — refresh (R)")

    def navigate(self, s: ZSession, t: Target, dry_assumed: int | None = None) -> None:
        """Validate, then go-to-tab-by-id + focus-pane-id, only with exactly one client.
        `dry_assumed`: under --dry-run, the client count a preceding (not executed) attach would
        have produced."""
        if not t.navigates:
            return
        tab_id, pane = self.validate(s, t)
        if self.runner.dry_run and dry_assumed is not None:
            n = dry_assumed
        else:
            n = self.clients(s)                 # immediately before acting
        if n != 1:
            raise _Fail(f"{n} clients attached to {s.name}: zellij would pick an arbitrary one — "
                        f"tab/pane not changed")
        if tab_id is not None:
            r = self.runner.act(*self.zj(s, "go-to-tab-by-id", str(tab_id)))
            if not r.ok:
                raise _Fail(f"go-to-tab-by-id {tab_id}: {(r.stderr or r.stdout).strip() or r.code}")
        if pane is not None:
            if self.runner.dry_run:                     # logged only: nothing to read back
                self.runner.act(*self.zj(s, "focus-pane-id", pane))
                return
            why = self.ensure_focus(s, pane)
            if why:
                self.focus_miss = (pane, why)

    # ---------------------------------------------------------------- pane focus (read back)
    def focused_pane(self, s: ZSession) -> str | None:
        """The pane the single attached client has focused (`list-clients`, ZELLIJ_PANE_ID column),
        e.g. "terminal_3"; None if not exactly one client or the query fails."""
        r = self.runner.query(*self.zj(s, "list-clients"))
        if not r.ok:
            return None
        rows = [ln.split() for ln in (r.stdout or "").splitlines()]
        rows = [x for x in rows if x and x[0].isdigit()]
        if len(rows) != 1 or len(rows[0]) < 2:
            return None
        return rows[0][1]

    def layer_visible(self, s: ZSession, tab_id: int) -> bool | None:
        """`are_floating_panes_visible` of a tab from a fresh `list-tabs`; None if unknown."""
        r = self.runner.query(*self.zj(s, "list-tabs", "--all", "--json"))
        if not r.ok:
            return None
        try:
            data = json.loads(r.stdout or "[]")
        except ValueError:
            return None
        for t in data if isinstance(data, list) else []:
            if isinstance(t, dict) and t.get("tab_id") == tab_id:
                v = t.get("are_floating_panes_visible")
                return v if isinstance(v, bool) else None
        return None

    def own_zid(self, s: ZSession) -> str | None:
        """agentree's own pane ("terminal_N") if agentree runs inside `s`."""
        if self.env.pane_id and self.env.pane_id.isdigit() and self.env.is_current(s.name, s.socket_dir):
            return f"terminal_{self.env.pane_id}"
        return None

    def _focus_pane_id(self, s: ZSession, pane: str) -> None:
        r = self.runner.act(*self.zj(s, "focus-pane-id", pane))
        if not (r.ok or already_focused(r, pane)):
            raise _Fail(f"focus-pane-id {pane}: {(r.stderr or r.stdout).strip() or r.code}")

    def ensure_focus(self, s: ZSession, pane: str) -> str | None:
        """After go-to-tab-by-id: focus `pane` and make sure the client really focuses it (see the
        module doc). Returns None when it does, else the reason. Raises _Fail only when
        focus-pane-id itself fails.

        The focus state is read BEFORE any focus mutation (list-clients + a fresh listing + the
        tab's floating-layer visibility):
          * already focused: nothing to do;
          * floating target (or a plugin): direct focus-pane-id, read back;
          * tiled target, shown floating layer with any live floating pane other than agentree's own
            (plugins included): no focus-pane-id, no toggle (either would hide them) — reported;
          * tiled target, live floating panes in the tab but the layer's visibility unknown
            (list-tabs failed, field missing, tab gone): no mutation — reported;
          * tiled target, agentree's own popup the only floating pane of a shown layer: a tracked
            toggle-floating-panes (toggled=True) first, restored if focus then fails;
          * otherwise (no shown layer / no live floating panes): focus-pane-id, then the bounded
            focus-next-pane walk, re-validated before every step (sole client, in the target
            tab, on a tiled pane; target still there).
        Everything runs under one deadline (focus_deadline_s)."""
        deadline = time.monotonic() + self.focus_deadline_s
        cur = self.focused_pane(s)
        if cur == pane:
            return None
        if cur is None:
            return "cannot read the client's focus"
        try:
            _tabs, panes = self.fresh_inventory(s)
        except _Fail:
            return "cannot list its tab"
        target = next((p for p in panes if p.zid == pane and not p.exited), None)
        if target is None:
            return "it is gone"
        if target.floating or target.is_plugin:
            self._focus_pane_id(s, pane)                # focus-pane-id does focus floating panes
            if self.focused_pane(s) == pane:
                return None
            return "floating pane" if target.floating else "not a terminal pane"
        tab = target.tab_id
        now = next((p for p in panes if p.zid == cur), None)
        if now is None or now.tab_id != tab:
            return "the client is not in its tab"
        floats = [p for p in panes if p.tab_id == tab and p.floating and not p.exited]
        shown = False
        if floats:
            # fail closed: with live floating panes, a tiled focus mutation needs the layer's
            # visibility to be known (query failed / field missing / tab gone -> no mutation)
            vis = self.layer_visible(s, tab)
            if vis is None:
                return "could not determine floating layer visibility"
            shown = vis or now.floating
        toggled = False
        if shown:
            own = self.own_zid(s)
            users = [p for p in floats if p.zid != own]
            if users:
                return (f"floating panes are shown in this tab ({', '.join(p.zid for p in users)}); "
                        f"not hiding them — hide them ({HIDE_FLOATING_KEYS}) and retry")
            r = self.runner.act(*self.zj(s, "toggle-floating-panes"))   # only agentree's own popup: hide it
            if not r.ok:
                return "could not hide agentree's popup"
            toggled = True
        try:
            why = self._walk(s, pane, tab, deadline)
        except _Fail:
            if toggled:
                self._restore_floating(s, tab)
            raise
        if why and toggled:
            self._restore_floating(s, tab)
        return why

    def _walk(self, s: ZSession, pane: str, tab: int, deadline: float) -> str | None:
        """focus-pane-id (a no-op for tiled panes in 0.45.1, kept for later versions), then the
        bounded, re-validated focus-next-pane walk. The floating layer is not shown here."""
        self._focus_pane_id(s, pane)
        bound: int | None = None
        steps = 0
        while True:
            cur = self.focused_pane(s)
            if cur == pane:
                return None
            if cur is None:
                return "the client changed"
            if time.monotonic() >= deadline:
                return f"timed out after {self.focus_deadline_s:g}s"
            try:
                _tabs, panes = self.fresh_inventory(s)
            except _Fail:
                return "cannot list its tab"
            target = next((p for p in panes if p.zid == pane and not p.exited), None)
            if target is None:
                return "it is gone"
            if target.tab_id != tab or target.floating:
                return "it moved"
            now = next((p for p in panes if p.zid == cur), None)
            if now is None or now.tab_id != tab:
                return "the client left its tab"
            if now.floating:
                return "floating panes are shown in that tab"
            if bound is None:
                bound = len([p for p in panes if p.tab_id == tab and not p.is_plugin and not p.floating
                             and p.selectable]) + 1
            if steps >= bound:
                return "stacked or not reachable with focus-next-pane"
            r = self.runner.act(*self.zj(s, "focus-next-pane"))
            steps += 1
            if not r.ok:
                return "focus-next-pane failed"

    def _restore_floating(self, s: ZSession, tab: int) -> None:
        """Show agentree's popup again after a failed focus, but only while the client is still on a
        tiled pane of the same tab and the layer is still hidden (our toggle is still in effect)."""
        cur = self.focused_pane(s)
        try:
            _tabs, panes = self.fresh_inventory(s)
        except _Fail:
            return
        now = next((p for p in panes if p.zid == cur), None) if cur else None
        if now is None or now.tab_id != tab or now.floating or self.layer_visible(s, tab) is not False:
            return                                  # only an explicitly hidden layer is ours to show
        self.runner.act(*self.zj(s, "toggle-floating-panes"))

    def _zellij_done(self) -> None:
        cb, self.on_zellij_done = self.on_zellij_done, None
        if cb is not None:
            cb()

    # ---------------------------------------------------------------- kitty
    def launch_socket(self) -> str:
        lo = self.env.kitty_listen_on
        if lo and self.owned(lo):
            return lo
        socks = self.sockets()
        if len(socks) == 1:
            return socks[0]
        if not socks:
            raise _Fail("no kitty remote-control socket (/tmp/kitty.sock-<pid>) found")
        raise _Fail(f"several kitty instances ({', '.join(socks)}) and none hosts agentree — refusing to guess")

    def _ls(self, sock: str) -> list:
        r = self.runner.query(self.kitty_cmd(sock, "ls"))
        if not r.ok:
            raise _Fail(f"kitty {sock}: ls failed: {(r.stderr or '').strip()[:120] or r.code}")
        try:
            data = json.loads(r.stdout)
        except ValueError as e:
            raise _Fail(f"kitty {sock}: bad ls output: {e}") from None
        return data if isinstance(data, list) else []

    @staticmethod
    def _windows(data: list):
        for ow in data:
            for tab in ow.get("tabs") or []:
                for win in tab.get("windows") or []:
                    yield win

    def revalidate(self, host: KittyHost, session: str) -> bool:
        """True if the cached window still hosts `session`, False if it (or its socket) is gone /
        not owned. Raises on contradiction (the window now shows something else)."""
        from agentree.sources import kitty as ksrc

        if not self.owned(host.socket):
            return False
        for win in self._windows(self._ls(host.socket)):
            if win.get("id") == host.window_id:
                if ksrc.resolve_window(win, {session}) == session:
                    return True
                raise _Fail(f"kitty window {host.window_id} no longer shows {session} — refresh (R)")
        return False

    def find_host(self, session: str) -> KittyHost | None:
        """A kitty window hosting `session`, discovered fresh over the owned sockets."""
        from agentree.sources import kitty as ksrc

        for sock in self.sockets():
            if not self.owned(sock):
                continue
            try:
                data = self._ls(sock)
            except _Fail:
                continue
            for win in self._windows(data):
                if ksrc.resolve_window(win, {session}) == session:
                    return KittyHost(sock, 0, 0, int(win.get("id", 0)), session)
        return None

    def focus_window(self, sock: str, window_id: int) -> None:
        r = self.runner.act(self.kitty_cmd(sock, "focus-window", "--match", f"id:{window_id}"))
        if not r.ok:
            raise _Fail(f"kitty focus-window {window_id}: {(r.stderr or '').strip()[:160] or r.code}")

    def launch_attach(self, sock: str, name: str, socket_dir: str) -> int | None:
        argv = self.kitty_cmd(sock, "launch", "--type=tab", "--tab-title", name,
                              "--var", f"agentree_session={name}", "--env", f"ZELLIJ_SOCKET_DIR={socket_dir}",
                              self.zellij, "attach", name)
        r = self.runner.act(argv, timeout=5.0)
        if not r.ok:
            raise _Fail(f"kitty launch: {(r.stderr or '').strip()[:160] or r.code}")
        out = r.stdout.strip()
        return int(out) if out.isdigit() else None

    def host_for(self, node: SessionNode, name: str) -> KittyHost | None:
        """Cached host if still valid (owned socket + corroborated), else fresh discovery."""
        host = node.kitty
        if host is not None and host.session == name and self.revalidate(host, name):
            return host
        return self.find_host(name)

    # ---------------------------------------------------------------- jump
    def go(self, t: Target) -> ActionResult:
        try:
            return self._go(t)
        except _Fail as e:
            return self.fail(e)

    def live_session(self, t: Target) -> tuple[ZSession, Fresh]:
        """Fresh identity check; returns the session as it is live now (socket dir from the fresh
        listing) or raises. Cached-exited-but-live-now sessions come back without tab targets."""
        s = t.node.session
        fresh = self.fresh_session(s.name)
        if fresh.live_dir is None:
            return s, fresh
        if s.state != "exited":
            if not s.inventory_complete:
                raise _Fail(f"{s.name}: inventory incomplete — refusing to navigate (see warnings)")
            self.check_incarnation(s, fresh, t.taken_at)
        return dataclasses.replace(s, state="attached", socket_dir=fresh.live_dir), fresh

    def _go(self, t: Target) -> ActionResult:
        s, fresh = self.live_session(t)
        if fresh.live_dir is None:
            if t.node.session.state != "exited":
                raise _Fail(f"{s.name} has exited — refresh (R)")
            sock = self.launch_socket()
            self.launch_attach(sock, s.name, self.env.exited_socket_dir())
            return self.finish(True, f"resurrecting {s.name}")
        if t.node.session.state == "exited":
            t = dataclasses.replace(t, tab_id=None, pane=None, worktree=None, agent_pid=None)
            self.notes.append(f"{s.name} is live again")
        if self.env.is_current(s.name, s.socket_dir):
            # agentree runs inside S: zellij navigation only; the floating agentree pane closes on exit
            self.navigate(s, t)
            return self.finish(True, t.label or s.name)
        host = self.host_for(t.node, s.name)
        if host is not None:
            self.navigate(s, t)
            self._zellij_done()                     # zellij part done: kitty-only from here
            self.focus_window(host.socket, host.window_id)
            return self.finish(True, t.label)
        if self.clients(s) == 0:
            sock = self.launch_socket()
            wid = self.launch_attach(sock, s.name, s.socket_dir)
            n = self.wait_for_client(s)
            self.navigate(s, t, dry_assumed=n)
            self._zellij_done()
            if wid is not None:
                self.focus_window(sock, wid)
            return self.finish(True, t.label)
        # attached somewhere agentree cannot see (another terminal): navigate there, nothing to focus
        if not t.navigates:
            raise _Fail(f"{s.name} is attached outside kitty — no window to focus")
        self.navigate(s, t)
        self.notes.append(f"{s.name} is attached outside kitty — no window to focus")
        return self.finish(True, t.label, close=False)


def already_focused(r, pane: str) -> bool:
    """`focus-pane-id` exit 2 + the exact 'already focused' diagnostic for the requested pane."""
    if r.code != 2 or not pane.startswith("terminal_"):
        return False
    n = pane[len("terminal_"):]
    return (r.stderr or "").strip() == f"Pane Terminal({n}) is already focused"
