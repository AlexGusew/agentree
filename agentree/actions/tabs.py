"""Open a zellij tab for a worktree (`t`), duplicate-safe. `ensure_tab` is the shared primitive
(also for M3 spawn and M4 `o`).

Findings: duplicate-sensitive creates must be blocked when the inventory is incomplete and
re-validated under a lock right before creating. `ensure_tab` holds
`<state>/locks/tab-<identity>.lock` (fcntl; identity = sha1 of canonical socket dir + session
name) for the whole check-attach-recheck-create sequence: list tabs+panes fresh; if a terminal
pane already sits in the worktree (deepest known worktree root wins) return that tab; otherwise
make sure a client is attached, list again, re-query clients and only then `new-tab`.

The lock only serialises agentree instances. Tabs created outside agentree (zellij UI, scripts, a user
typing `cd` in another tab) are outside the lock: they are caught by the fresh listing, but a
creation racing between that listing and `new-tab` cannot be excluded.

zellij 0.45.1 (verified live): `action new-tab` in a session with *no client attached* fails
("Not enough room for panes") yet still prints a tab id, and the next attach panics the server.
So `new-tab` never runs without a client, and the printed id is verified: absent before, present
after, with the requested name and a pane inside the worktree.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import time
from dataclasses import dataclass
from typing import Iterator

from agentree.actions.api import ActionResult
from agentree.actions.jump import Navigator, Target, _Fail, deepest, find_session, known_worktrees
from agentree.actions.runner import identity_key
from agentree.types import Row, SessionNode, Snapshot, WorktreeNode, ZSession

LOCK_WAIT_S = 5.0
VERIFY_S = 1.0


def lock_path(state_dir: str, socket_dir: str | None, session: str) -> str:
    return os.path.join(state_dir, "locks", f"tab-{identity_key(socket_dir, session)}.lock")


def _sha1(text: str) -> str:
    try:
        from _sha1 import sha1
    except ImportError:  # pragma: no cover
        from hashlib import sha1
    return sha1(text.encode()).hexdigest()[:16]


def repo_lock_path(state_dir: str, repo: str) -> str:
    """Creation lock per repo: held for a whole spawn execute and a worktree remove."""
    return os.path.join(state_dir, "locks", f"repo-{_sha1(os.path.realpath(repo))}.lock")


def session_name_lock_path(state_dir: str, name: str) -> str:
    """Creation lock per *new* session name: absence check -> launch -> client wait."""
    return os.path.join(state_dir, "locks", f"session-{_sha1(name)}.lock")


@contextlib.contextmanager
def named_lock(path: str, wait_s: float | None = None, busy: str = "another agentree is busy here — try again"
               ) -> Iterator[None]:
    """Exclusive fcntl lock on `path`, waiting up to `wait_s` (default LOCK_WAIT_S); raises
    _Fail(busy) after that."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _Fail(busy) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def session_lock(path: str, wait_s: float = LOCK_WAIT_S) -> Iterator[None]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _Fail("another agentree is opening a tab in this session — try again") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def tab_name(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


@dataclass(frozen=True)
class TabResult:
    ok: bool
    tab_id: int | None = None
    created: bool = False
    message: str = ""
    clients: int = 0                    # clients attached when the tab was found/created
    verified: bool = False              # created and confirmed (listed, named, pane in the worktree)


def _existing(tabs, panes, worktree: str, roots) -> tuple[int, str] | None:
    for p in panes:
        if not p.is_plugin and not p.exited and deepest(p.cwd, roots) == worktree:
            return p.tab_id, next((t.name for t in tabs if t.tab_id == p.tab_id), str(p.tab_id))
    return None


def ensure_tab(nav: Navigator, s: ZSession, worktree: str, name: str, *, state_dir: str,
               layout: str | None = None, roots=frozenset(), attach: bool = True,
               layout_string: str | None = None, focus: bool = True,
               before_create=None, on_created=None) -> TabResult:
    """Find or create the tab for `worktree` in live session `s` (socket_dir = fresh live dir).

    Under the session lock: fresh listing -> existing tab? -> ensure a client (kitty attach if
    `attach`) -> fresh listing again -> clients re-queried -> `new-tab` (`--no-focus` with several
    clients or `focus=False`; `layout_string` (raw KDL) wins over the `layout` name) -> verify.
    `before_create()` runs right before `new-tab` (spawn journals the pending tab there);
    `on_created(tab_id)` runs as soon as `new-tab` printed an id, before any further query. That id
    is returned (created=True, ok=False, verified=False) on every later failure path — verification
    mismatch, a failing re-listing, any exception — so callers can always account for the tab.
    An id that already existed before `new-tab` is refused without calling `on_created`.
    Raises nothing once a tab id is known; failures come back as TabResult(ok=False, message).
    """
    roots = frozenset(roots) | {worktree}
    created: list[int] = []                 # the id new-tab printed, once known
    try:
        with session_lock(lock_path(state_dir, s.socket_dir, s.name)):
            tabs, panes = nav.fresh_inventory(s)
            hit = _existing(tabs, panes, worktree, roots)
            if hit:
                return TabResult(True, hit[0], False, f"already open in tab {hit[1]}")
            clients = nav.clients(s)
            if clients == 0:
                if not attach:
                    return TabResult(False, message=f"{s.name}: no client attached")
                sock = nav.launch_socket()
                nav.launch_attach(sock, s.name, s.socket_dir or nav.env.exited_socket_dir())
                nav.wait_for_client(s)
                if not nav.runner.dry_run:
                    tabs, panes = nav.fresh_inventory(s)          # re-check after attachment
                    hit = _existing(tabs, panes, worktree, roots)
                    if hit:
                        return TabResult(True, hit[0], False, f"already open in tab {hit[1]}")
            clients = nav.clients(s) if not nav.runner.dry_run else max(clients, 1)  # right before new-tab
            if clients == 0:
                return TabResult(False, message=f"{s.name}: no client attached — not creating a tab")
            before = {t.tab_id for t in tabs}
            args = ["new-tab", "--cwd", worktree, "--name", name]
            if layout_string:
                args += ["--layout-string", layout_string]
            elif layout:
                args += ["--layout", layout]
            if clients > 1 or not focus:
                args.append("--no-focus")
            if before_create is not None:
                before_create()
            r = nav.runner.act(*nav.zj(s, *args))
            out = (r.stdout or "").strip()
            if out.isdigit() and int(out) in before and not nav.runner.dry_run:
                return TabResult(False, message=f"new-tab reported existing tab {out} — refusing")
            if out.isdigit() and not nav.runner.dry_run:
                created.append(int(out))
                if on_created is not None:
                    on_created(int(out))        # persisted by the caller before any further query
            if not r.ok:
                return TabResult(False, created[0] if created else None, bool(created),
                                 f"new-tab: {(r.stderr or r.stdout).strip()[:160] or r.code}")
            if nav.runner.dry_run:
                return TabResult(True, None, True, "", clients)
            if not out.isdigit():
                return TabResult(False, message=f"new-tab printed no tab id ({out[:40]!r})")
            tab_id = int(out)
            # the new tab's pane can be listed a moment after the tab: re-list for up to VERIFY_S
            deadline = time.monotonic() + VERIFY_S
            while True:
                tabs, panes = nav.fresh_inventory(s)
                tab = next((t for t in tabs if t.tab_id == tab_id), None)
                mine = [p for p in panes if p.tab_id == tab_id and not p.is_plugin]
                if tab is not None and tab.name == name and any(deepest(p.cwd, roots) == worktree for p in mine):
                    return TabResult(True, tab_id, True, "", clients, verified=True)
                if time.monotonic() >= deadline:
                    break
                nav.sleep(0.1)
            if tab is None:
                return TabResult(False, tab_id, True, f"new-tab reported tab {tab_id} but zellij does not list it")
            seen = ", ".join(f"{p.zid}:{p.cwd}" for p in mine) or "no panes"
            return TabResult(False, tab_id, True, f"tab {tab_id} ({tab.name!r}; {seen}) was created but "
                                                  f"does not look like {name} in {worktree}")
    except _Fail as e:
        if created:
            return TabResult(False, created[0], True, f"tab {created[0]} created but not verified: {e}")
        return TabResult(False, message=str(e))
    except Exception as e:  # noqa: BLE001 - after new-tab printed an id, never lose it
        if created:
            return TabResult(False, created[0], True,
                             f"tab {created[0]} created but not verified: {type(e).__name__}: {e}")
        raise


def target_session(nav: Navigator, row: Row, snap: Snapshot) -> tuple[SessionNode, ZSession]:
    """Row's session if live now, else the session agentree runs in; identity checked fresh."""
    node = find_session(snap, row.session)
    if node is not None and node.session.state != "exited":
        fresh = nav.fresh_session(node.session.name)
        if fresh.live_dir is not None:
            if not node.session.inventory_complete:
                raise _Fail(f"{node.session.name}: inventory incomplete — refusing to create a tab")
            nav.check_incarnation(node.session, fresh, snap.taken_at)
            return node, dataclasses.replace(node.session, socket_dir=fresh.live_dir)
    cur = nav.env.current_session
    if not cur:
        raise _Fail("no live session for this worktree and agentree is not running inside zellij")
    fresh = nav.fresh_session(cur)
    if fresh.live_dir is None or not nav.env.is_current(cur, fresh.live_dir):
        raise _Fail(f"current session {cur} is not live in {nav.env.current_dir()} — refusing")
    node = find_session(snap, cur)
    if node is not None and not node.session.inventory_complete:
        raise _Fail(f"{cur}: inventory incomplete — refusing to create a tab")
    s = ZSession(cur, "attached", fresh.age_s, fresh.live_dir, 1, True)
    return node or SessionNode(s), s


def open_tab(nav: Navigator, row: Row, snap: Snapshot, *, state_dir: str, layout: str | None = None
             ) -> ActionResult:
    try:
        return _open_tab(nav, row, snap, state_dir=state_dir, layout=layout)
    except _Fail as e:
        return nav.fail(e)


def _open_tab(nav: Navigator, row: Row, snap: Snapshot, *, state_dir: str, layout: str | None) -> ActionResult:
    if row.kind != "worktree" or not isinstance(row.ref, WorktreeNode):
        raise _Fail("open tab: select a worktree")
    wt = row.ref.worktree
    if not os.path.isdir(wt.path):
        raise _Fail(f"{wt.path} does not exist")
    node, s = target_session(nav, row, snap)
    roots = known_worktrees(snap)
    name = tab_name(wt.path)
    res = ensure_tab(nav, s, wt.path, name, state_dir=state_dir, layout=layout, roots=roots)
    if not res.ok:
        raise _Fail(res.message)
    if nav.runner.dry_run:
        return nav.finish(True, "")
    label = f"{s.name} › {name}"
    if res.message:
        nav.notes.append(res.message)
    if res.created and res.clients > 1:
        nav.notes.append(f"{res.clients} clients attached: tab created without switching to it")
        return nav.finish(True, label, close=False)
    node = dataclasses.replace(node, session=s)
    return nav.go(Target(node, tab_id=res.tab_id, label=label, worktree=wt.path, roots=roots,
                         taken_at=None))
