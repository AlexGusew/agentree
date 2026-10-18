"""`o`: a new agent pane (any registered provider) in the selected worktree's tab (and the
shared "command pane in a worktree's tab" primitive, also used by `g` lazygit).

Flow (zellij 0.45.1, verified live):
  0. `o` only: the provider (agentree.providers.registry) builds the pane command
     (build_launch(mode="pane")); its setup steps run through the runner first;
  1. worktree of the row: worktree / orphan worktree row, or the deepest known worktree holding an
     agent's / pane's cwd;
  2. session as for `t` (row's session if live, else the session agentree runs in), identity checked
     fresh; more than one client attached -> refuse before creating anything;
  3. `ensure_tab` (its own session lock, client presence, re-validation; attaches a kitty client
     if none);
  4. under the session action lock (tabs.session_lock, same file as ensure_tab) for steps 4-6:
     fresh listing — the tab id still exists and still holds the worktree (an unknown
     `--tab-id` exits 0 and prints `terminal_<N>` for a pane that never appears, so the id is
     validated right before creating); the pane ids present now are remembered; clients
     re-queried immediately before creating: exactly one, else refuse;
  5. `new-pane --tab-id <id> --cwd <wt> --name <name> [--floating --close-on-exit] -- <cmd>`:
     explicit placement, so a tab switch by the user cannot misplace the pane. The printed
     `terminal_<N>` must be absent from the pre-create listing and is verified in a fresh one:
     present, in the requested tab, cwd inside the worktree (floating when asked for); no
     printed id is a refusal;
  6. focus, separately: Navigator.go — go-to-tab-by-id -> focus read back from `list-clients`
     before any focus mutation -> Navigator.ensure_focus (floating target: focus-pane-id; tiled
     target: never anything that would hide a user's shown floating layer; bounded, re-validated
     `focus-next-pane` walk) -> the session lock is released (Navigator.on_zellij_done) -> kitty
     window. If the pane cannot be focused the result says so (ok, popup kept open, refresh): the
     pane exists where it belongs;
  7. `o` only: links.json record for the worktree, merged under one links.lock (agentPanes;
     pids best effort: poll up to AGENT_WAIT_S for the provider's agent process whose
     ZELLIJ_PANE_ID is the new pane — provider.find_launched_pids, which may prefer the process
     carrying the launch's session id). Only a record of this session (name + socketDir) is updated, or one whose session
     is verifiably this session renamed (verify_rename, whose RenameEvidence — the record's
     ownership fields and the witness agents with start times — must still hold under the lock,
     else "link changed concurrently; agent not recorded"); any other session's record is left
     untouched ("link kept for <B>; agent not recorded"). Records written here carry socketDir
     and sessionStarted (epoch start of the session) for that check.
The session lock serialises cooperating agentree actions only; what the user does in zellij (switching
tabs, closing panes) is outside it — hence the explicit --tab-id and the re-validation.
Dry-run logs the commands up to new-pane and stops (the pane does not exist to verify / jump to).
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from agentree.actions.api import ActionResult, AgentChoice, AgentOptions
from agentree.actions.jump import AGE_TOLERANCE_S, Navigator, Target, _Fail, deepest, known_worktrees
from agentree.actions.runner import canonical_dir
from agentree.types import Agent, Row, Snapshot, WorktreeNode, ZPane

VERIFY_S = 2.0
AGENT_WAIT_S = 3.0
_PANE_RE = re.compile(r"terminal_(\d+)")


@dataclass(frozen=True)
class PaneSpec:
    what: str                       # for refusals: "an agent", "lazygit"
    noun: str                       # for results: "agent", "lazygit"
    name: str                       # zellij pane name (--name)
    command: tuple[str, ...]        # resolved argv
    floating: bool = False


@dataclass(frozen=True)
class Opened:
    session: str
    socket_dir: str | None
    tab_id: int
    tab_name: str
    pane_id: int
    worktree: str
    tab_created: bool


# --------------------------------------------------------------------------- helpers

def provider_available(provider: Any, cfg: Any) -> bool:
    """The provider's launch command resolves (computed on the action worker). A provider that
    cannot even answer counts as unavailable."""
    try:
        return provider.resolve_command(cfg) is not None
    except Exception:  # noqa: BLE001 - one broken provider must not break the choices
        return False


def agent_choices(cfg: Any) -> tuple[AgentChoice, ...]:
    """Every registered agent provider, in registry order, with its availability."""
    from agentree.providers import registry

    out: list[AgentChoice] = []
    for s in registry.specs():
        try:
            prov = registry.get(s.id)
        except Exception:  # noqa: BLE001 - import failure: offered as unavailable
            out.append(AgentChoice(s.id, s.label, False))
            continue
        out.append(AgentChoice(s.id, s.label, provider_available(prov, cfg)))
    return tuple(out)


def agent_options(cfg: Any) -> AgentOptions:
    """Choices for `o` (agent here): every registered provider + availability, and the default."""
    from agentree.providers import registry

    return AgentOptions(choices=agent_choices(cfg), default_provider=registry.default_id(cfg))


def get_provider(provider_id: Any) -> Any:
    """The registered provider `provider_id`; raises _Fail for an unknown one (fail closed: never
    reinterpreted as another provider)."""
    from agentree.providers import registry

    if not isinstance(provider_id, str):
        raise _Fail(f"unknown agent provider {provider_id!r}")
    try:
        return registry.get(provider_id)
    except KeyError:
        raise _Fail(f"unknown agent provider {provider_id!r}") from None


def pid_finder(provider: Any, session_id: str | None, timeout_s: float = 1.0
               ) -> Callable[[str, set[int]], list[int]]:
    """(session, pane ids) -> pids of the provider's agents launched there (provider-defined;
    e.g. preferring the process carrying our pre-generated session id)."""
    def find(session: str, pane_ids: set[int]) -> list[int]:
        return list(provider.find_launched_pids(session, frozenset(pane_ids), session_id=session_id,
                                                timeout_s=timeout_s))
    return find


def worktree_of_row(row: Row | None, snap: Snapshot) -> str:
    """The worktree a row stands for; raises _Fail with a user-facing reason."""
    if row is None:
        raise _Fail("select a worktree, agent or pane")
    if row.kind == "worktree" and isinstance(row.ref, WorktreeNode):
        return row.ref.worktree.path
    cwd = None
    if row.kind == "agent" and isinstance(row.ref, Agent):
        cwd = row.ref.cwd
    elif row.kind == "pane" and isinstance(row.ref, ZPane):
        cwd = row.ref.cwd
    else:
        raise _Fail("select a worktree, agent or pane")
    wt = deepest(cwd, known_worktrees(snap))
    if wt is None:
        raise _Fail(f"{cwd or 'its directory'} is not inside a known worktree")
    return wt


def _inside(cwd: str | None, wt: str) -> bool:
    if not cwd:
        return False
    c, w = os.path.realpath(cwd), os.path.realpath(wt)
    return c == w or c.startswith(w.rstrip("/") + "/")


def _too_many(n: int, s_name: str, what: str, extra: str = "") -> _Fail:
    if n == 0:
        return _Fail(f"no zellij client attached to {s_name}{extra} — not opening {what}")
    return _Fail(f"{n} clients attached to {s_name}: zellij would pick an arbitrary one{extra} — "
                 f"not opening {what}; detach the others or run agentree inside {s_name}")


# --------------------------------------------------------------------------- primitive

def open_pane(nav: Navigator, row: Row, snap: Snapshot, spec: PaneSpec, *, state_dir: str,
              tab_layout: str | None = None) -> tuple[Opened | None, ActionResult]:
    """Steps 1-6 of the module doc: (the verified pane or None, the result to show). Never raises."""
    from agentree.actions.tabs import ensure_tab, lock_path, session_lock, tab_name, target_session

    try:
        wt = worktree_of_row(row, snap)
        if not os.path.isdir(wt):
            raise _Fail(f"{wt} does not exist")
        node, s = target_session(nav, row, snap)
        roots = known_worktrees(snap) | {wt}
        n = nav.clients(s)
        if n > 1:
            raise _too_many(n, s.name, spec.what)
        res = ensure_tab(nav, s, wt, tab_name(wt), state_dir=state_dir, layout=tab_layout, roots=roots)
        if not res.ok:
            raise _Fail(res.message)
        with contextlib.ExitStack() as held:
            held.enter_context(session_lock(lock_path(state_dir, s.socket_dir, s.name)))
            o = _create(nav, s, spec, wt, roots, res.tab_id, res.created)
            if o is None:                                       # dry-run
                return None, nav.finish(True, "")
            # verify -> create -> zellij navigation under the lock; kitty-only focus after it
            nav.on_zellij_done = held.close
            try:
                return o, _focus(nav, snap, o, spec)
            finally:
                nav.on_zellij_done = None
    except _Fail as e:
        return None, nav.fail(e)


def _create(nav: Navigator, s, spec: PaneSpec, wt: str, roots, tab_id: int | None, tab_created: bool
            ) -> Opened | None:
    """Steps 4-5 (caller holds the session lock). Raises _Fail."""
    from agentree.actions.tabs import tab_name

    dry = nav.runner.dry_run
    made = f" (tab {tab_name(wt)} was created)" if tab_created else ""
    before: set[int] = set()
    tname = tab_name(wt)
    if not dry:
        if tab_id is None:
            raise _Fail(f"{s.name}: no tab id for {tname} — refusing")
        tabs, panes = nav.fresh_inventory(s)            # validate the tab id right before creating
        tab = next((t for t in tabs if t.tab_id == tab_id), None)
        if tab is None:
            raise _Fail(f"{s.name}: tab {tab_id} is gone — refresh (R)")
        tname = tab.name
        if not any(p.tab_id == tab_id and not p.is_plugin and not p.exited and deepest(p.cwd, roots) == wt
                   for p in panes):
            raise _Fail(f"{s.name}: tab {tname} no longer holds {os.path.basename(wt)} — refresh (R)")
        before = {p.pane_id for p in panes if not p.is_plugin}
        n = nav.clients(s)                               # immediately before new-pane
        if n != 1:
            raise _too_many(n, s.name, spec.what, made)
    args = ["new-pane", "--tab-id", str(tab_id) if tab_id is not None else "<new tab>",
            "--cwd", wt, "--name", spec.name]
    if spec.floating:
        args += ["--floating", "--close-on-exit"]
    args += ["--", *spec.command]
    r = nav.runner.act(*nav.zj(s, *args))
    if not r.ok:
        raise _Fail(f"new-pane: {((r.stderr or r.stdout) or '').strip()[:160] or r.code}{made}")
    if dry:
        return None
    out = (r.stdout or "").strip()
    m = _PANE_RE.fullmatch(out)
    if m is None:                                        # nothing to verify the pane's identity by
        raise _Fail(f"new-pane printed no pane id ({out[:40]!r}) in {s.name} tab {tname} — not focusing "
                    f"anything; close the pane yourself if it is there{made}")
    want = int(m.group(1))
    if want in before:
        raise _Fail(f"new-pane reported terminal_{want}, which existed before — refusing to treat it as "
                    f"the new pane{made}")
    deadline = time.monotonic() + VERIFY_S
    why = "it is not listed"
    while True:
        _tabs, panes = nav.fresh_inventory(s)
        cands = [p for p in panes if not p.is_plugin and p.pane_id == want]
        p = cands[0] if len(cands) == 1 else None
        if p is not None:
            if p.exited:
                why = "its command already exited"
            elif p.tab_id != tab_id:
                why = f"it opened in tab {p.tab_id}, not {tab_id}"
            elif spec.floating and not p.floating:
                why = "it is not floating"
            elif not p.cwd:
                why = "it reports no working directory"
            elif not _inside(p.cwd, wt):
                why = f"its cwd is {p.cwd}"
            else:
                return Opened(s.name, s.socket_dir, tab_id, tname, p.pane_id, wt, tab_created)
        elif len(cands) > 1:
            why = "it is listed more than once"
        if time.monotonic() >= deadline:
            break
        nav.sleep(0.1)
    raise _Fail(f"new-pane reported terminal_{want} in {s.name} tab {tname}, but {why} — not focusing it; "
                f"close it yourself if it is there{made}")


def _focus(nav: Navigator, snap: Snapshot, o: Opened, spec: PaneSpec) -> ActionResult:
    """Step 6: focus the verified pane (zellij tab, pane read back, kitty window)."""
    from agentree.actions.jump import find_session
    from agentree.types import SessionNode, ZSession

    label = f"{o.session} › {o.tab_name} › {spec.name}"
    node = find_session(snap, o.session)
    s = ZSession(o.session, "attached", None, o.socket_dir, 1, inventory_complete=True)
    node = dataclasses.replace(node, session=s) if node is not None else SessionNode(s)
    if o.tab_created:
        nav.notes.append(f"tab {o.tab_name} created")
    res = nav.go(Target(node, tab_id=o.tab_id, pane=f"terminal_{o.pane_id}", label=label,
                        roots=known_worktrees(snap) | {o.worktree}, taken_at=None))
    if nav.focus_miss is not None:
        pane, why = nav.focus_miss
        return ActionResult(True, f"{spec.noun} pane created in tab {o.tab_name} but focus failed: could not "
                                  f"focus pane {pane} ({why})", close=False, refresh=True)
    if not res.ok:
        return ActionResult(False, f"{label} opened in pane terminal_{o.pane_id}, but not focused: {res.message}",
                            refresh=True)
    return dataclasses.replace(res, refresh=True)


# --------------------------------------------------------------------------- `o`

def _rec_dir(rec: dict) -> str | None:
    v = rec.get("socketDir")
    return v if isinstance(v, str) and v else None


def _owned_by(rec: dict, o: Opened) -> bool:
    """The record names the action's session: same name, and the same socket dir if it has one
    (records written before socketDir existed only carry the name)."""
    if rec.get("session") != o.session:
        return False
    d = _rec_dir(rec)
    return d is None or canonical_dir(d) == canonical_dir(o.socket_dir)


def session_started(nav: Navigator, o: Opened) -> float | None:
    """Epoch start of the action's session (fresh list-sessions age; 1 s resolution), None if unknown."""
    try:
        f = nav.lookup_session(o.session)
    except _Fail:
        return None
    if f is None or f.live_dir is None or f.age_s is None or o.socket_dir is None:
        return None
    if canonical_dir(f.live_dir) != canonical_dir(o.socket_dir):
        return None
    return nav.clock() - f.age_s


@dataclass(frozen=True)
class RenameEvidence:
    """What verify_rename established, bound to the record it looked at: its ownership fields
    exactly as stored, and — when a running agent was relied on (socket dir taken from it, or the
    legacy incarnation proof) — those witnesses as (pid, process start time)."""
    session: str
    socket_dir: Any                 # the record's raw "socketDir" (None if absent)
    session_started: Any            # the record's raw "sessionStarted" (None if absent)
    witnesses: tuple[tuple[int, float], ...] = ()


def verify_rename(nav: Navigator, o: Opened, rec: dict, started: float | None
                  ) -> tuple[RenameEvidence | None, str | None]:
    """(evidence, None) if the record's session is verifiably the action's session under its old
    name (a zellij rename), else (None, the reason it is not). Required: the old name is live
    nowhere; the record's socket dir (stored, or from a still-running recorded agent's
    ZELLIJ_SOCKET_DIR) is the action session's; and the incarnation matches — the stored
    sessionStarted equals the action session's start (within AGE_TOLERANCE_S), or, for records
    without it, a recorded agent that still carries the old ZELLIJ_SESSION_NAME runs in a live
    pane of the action session. Witness agents count only with a known process start time."""
    old = rec.get("session")
    if not isinstance(old, str) or not old:
        return None, "the record names no session"
    if old == o.session:
        return None, f"it is {old} in {_rec_dir(rec)}"
    try:
        f = nav.lookup_session(old)
    except _Fail as e:
        return None, str(e)
    if f is not None and f.live_dir is not None:
        return None, f"{old} is live"
    witnesses: list[tuple[int, float, dict]] = []
    for pid in rec.get("agentPids") or []:
        if not isinstance(pid, int) or isinstance(pid, bool) or not nav.pid_alive(pid):
            continue
        env = nav.proc_env(pid)
        st0 = nav.proc_start(pid)
        if env and env.get("ZELLIJ_SESSION_NAME") == old and st0 is not None:
            witnesses.append((pid, st0, env))
    used: list[tuple[int, float]] = []
    rd = _rec_dir(rec)
    if rd is None:
        w = next((w for w in witnesses if w[2].get("ZELLIJ_SOCKET_DIR")), None)
        if w is None:
            return None, f"{old}'s socket dir is unknown"
        rd = w[2]["ZELLIJ_SOCKET_DIR"]
        used.append((w[0], w[1]))
    if o.socket_dir is None or canonical_dir(rd) != canonical_dir(o.socket_dir):
        return None, f"{old} lived in {rd}"

    def evidence() -> RenameEvidence:
        return RenameEvidence(old, rec.get("socketDir"), rec.get("sessionStarted"), tuple(dict.fromkeys(used)))
    st = rec.get("sessionStarted")
    if isinstance(st, (int, float)) and not isinstance(st, bool):
        if started is None:
            return None, f"{o.session}'s age is unknown"
        if abs(started - st) > AGE_TOLERANCE_S:
            return None, f"{o.session} is not the session {old} was"
        return evidence(), None
    from agentree.types import ZSession

    try:
        _tabs, panes = nav.fresh_inventory(ZSession(o.session, "attached", None, o.socket_dir, 1))
    except _Fail as e:
        return None, str(e)
    live = {p.pane_id for p in panes if not p.is_plugin and not p.exited}
    proof = [(pid, st0) for pid, st0, e in witnesses
             if canonical_dir(e.get("ZELLIJ_SOCKET_DIR") or rd) == canonical_dir(o.socket_dir)
             and (e.get("ZELLIJ_PANE_ID") or "").isdigit() and int(e["ZELLIJ_PANE_ID"]) in live]
    if not proof:
        return None, f"nothing shows {o.session} is the renamed {old}"
    used.extend(proof)
    return evidence(), None


def _evidence_holds(nav: Navigator, cur: dict, ev: RenameEvidence) -> bool:
    """Under links.lock: the record is still the one verify_rename looked at (same session,
    socketDir and sessionStarted, as stored) and every witness it relied on is still listed in
    agentPids and is still the same process (same start time)."""
    if (cur.get("session"), cur.get("socketDir"), cur.get("sessionStarted")) != (
            ev.session, ev.socket_dir, ev.session_started):
        return False
    listed = {p for p in cur.get("agentPids") or [] if isinstance(p, int) and not isinstance(p, bool)}
    for pid, st0 in ev.witnesses:
        if pid not in listed:
            return False
        now = nav.proc_start(pid)
        if now is None or abs(now - st0) > 0.001:
            return False
    return True


def record_link(nav: Navigator, o: Opened, pids: list[int], links, *, started: float | None = None,
                evidence: RenameEvidence | None = None) -> str | None:
    """Add the agent pane (and its pids) to the worktree's links.json record in one locked
    read-modify-write (state.merge_link); returns a note when nothing was recorded. Unrelated
    fields are kept.

    The record is updated only if it names the action's session (name + socket dir), or if it
    is still exactly the record a verified rename was established for (`evidence`, see
    _evidence_holds); then its session becomes the new name. Evidence that no longer holds:
    "link changed concurrently; agent not recorded". A record of any other session is left
    exactly as it is: "link kept for <B>; agent not recorded"."""
    notes: list[str] = []

    def mutate(cur: dict | None) -> dict | None:
        rec = dict(cur or {})
        if cur is not None and not _owned_by(cur, o):
            if evidence is None:
                notes.append(f"link kept for {cur.get('session')}; agent not recorded")
                return cur
            if not _evidence_holds(nav, cur, evidence):
                notes.append("link changed concurrently; agent not recorded")
                return cur
        rec.update(session=o.session, tabName=o.tab_name, tabId=o.tab_id)
        if o.socket_dir:
            rec["socketDir"] = canonical_dir(o.socket_dir)
        if started is not None:
            rec["sessionStarted"] = round(started, 1)
        old_pids = [p for p in rec.get("agentPids") or [] if isinstance(p, int)]
        old_panes = [p for p in rec.get("agentPanes") or [] if isinstance(p, int)]
        rec["agentPids"] = sorted(set(old_pids) | set(pids))
        rec["agentPanes"] = sorted(set(old_panes) | {o.pane_id})
        return rec
    try:
        nav.runner.internal(f"links.json: {o.session} › {o.tab_name} agent pane terminal_{o.pane_id} "
                            f"({o.worktree})", lambda: links.merge_link(o.worktree, mutate))
    except (OSError, ValueError) as e:
        return f"links.json not updated: {e}"
    return notes[0] if notes else None


def link_agent(nav: Navigator, o: Opened, pids: list[int], links) -> str | None:
    """Decide outside the links lock (zellij / process queries) whether a record of another
    session is a verified rename, then merge; record_link checks under the lock that the record
    is still the one the evidence was established for."""
    started = session_started(nav, o)
    evidence = None
    from agentree.model.fmt import norm

    try:
        cur = next((r for r in links.load_links()
                    if isinstance(r.get("worktreePath"), str) and norm(r["worktreePath"]) == norm(o.worktree)), None)
    except (OSError, ValueError):
        cur = None
    if cur is not None and not _owned_by(cur, o):
        evidence, _why = verify_rename(nav, o, cur, started)
    return record_link(nav, o, pids, links, started=started, evidence=evidence)


def new_agent(nav: Navigator, row: Row, snap: Snapshot, *, cfg: Any, state_dir: str, links=None,
              find_agents: Callable[[str, set[int]], list[int]] | None = None,
              provider: str | None = None) -> ActionResult:
    """`o`: an agent pane of `provider` (default: registry.default_id(cfg)) in the row's worktree
    tab. The provider builds the pane command (build_launch(mode="pane")) and its setup steps
    (e.g. a hooks settings file), which run through the runner (dry-run honoured) before the
    pane is opened; the pid poll is the provider's find_launched_pids (keyed by the launch's
    session id, if it has one). An unknown provider is refused before anything is touched."""
    from agentree.providers import registry
    from agentree.providers.base import LaunchRequest

    pid = provider if provider is not None else registry.default_id(cfg)
    try:
        prov = get_provider(pid)
    except _Fail as e:
        return nav.fail(e)
    if prov.resolve_command(cfg) is None:
        return nav.fail(_Fail(registry.missing_command_message(pid, cfg)))
    launch = prov.build_launch(cfg, LaunchRequest(mode="pane", state_dir=state_dir))
    if launch.errors:
        return nav.fail(_Fail(launch.errors[0]))
    if launch.argv is None:
        return nav.fail(_Fail(registry.missing_command_message(pid, cfg)))
    for step in launch.setup:
        try:
            prov.execute_setup(step, runner=nav.runner, state_dir=state_dir)
        except OSError as e:
            return nav.fail(_Fail(f"cannot {step.label}: {e}"))
    spec = PaneSpec("an agent", "agent", pid, tuple(launch.argv))
    o, res = open_pane(nav, row, snap, spec, state_dir=state_dir, tab_layout=getattr(cfg, "tab_layout", None))
    if o is None:
        return res
    if links is None:
        from agentree import state as links  # type: ignore[no-redef]
    if find_agents is None:
        find_agents = pid_finder(prov, launch.session_id)
    pids: list[int] = []
    deadline = time.monotonic() + AGENT_WAIT_S
    while True:
        try:
            pids = list(find_agents(o.session, {o.pane_id}))
        except Exception:  # noqa: BLE001 - best effort
            pids = []
        if pids or time.monotonic() >= deadline:
            break
        nav.sleep(0.2)
    note = link_agent(nav, o, pids, links)
    if note:
        res = dataclasses.replace(res, message=f"{res.message} ({note})")
    return res
