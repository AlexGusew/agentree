"""Detail pane content for the selected row. Pure."""
from __future__ import annotations

from agentree.types import (
    Agent,
    Detail,
    DetailAction,
    DetailField,
    Orphans,
    Row,
    Seg,
    SessionNode,
    Snapshot,
    SourceWarning,
    WorktreeNode,
    ZPane,
)

from . import fmt
from .tree import agent_status_seg, session_agent_count, session_state_seg


def _f(label: str, *segs: Seg, wrap: bool = False) -> DetailField:
    return DetailField(label, tuple(segs), wrap)


def _t(text: str, role: str = "text") -> Seg:
    return Seg(text, role)  # type: ignore[arg-type]


def _elapsed(snapshot: Snapshot, now: float) -> float:
    return max(0.0, now - snapshot.taken_at) if now else 0.0


def find_agent_worktree(snapshot: Snapshot, pid: int) -> tuple[str | None, WorktreeNode | None]:
    for sn in snapshot.sessions:
        for wn in sn.worktrees:
            if any(a.pid == pid for a in wn.agents):
                return sn.session.name, wn
        if any(a.pid == pid for a in sn.loose_agents):
            return sn.session.name, None
    return None, None


def find_pane_worktree(snapshot: Snapshot, session: str | None, pane: ZPane) -> WorktreeNode | None:
    for sn in snapshot.sessions:
        if session is not None and sn.session.name != session:
            continue
        for wn in sn.worktrees:
            if any(p.pane_id == pane.pane_id and p.is_plugin == pane.is_plugin for p in wn.panes):
                return wn
    return None


def _vs_label(base_ref: str | None) -> str:
    if not base_ref:
        return "vs main"
    short = base_ref.split("/", 1)[1] if base_ref.startswith("origin/") else base_ref
    return f"vs {short}"


def _session_detail(node: SessionNode, elapsed: float) -> Detail:
    s = node.session
    state = session_state_seg(s, elapsed)
    if s.state != "exited":
        state_segs = (state, _t(f" · {fmt.plural(s.clients, 'client')}", "dim"))
    else:
        state_segs = (state, _t(" · resurrectable", "dim"))
    n_agents = session_agent_count(node)
    layout = " · ".join(t.name for t in sorted(s.tabs, key=lambda t: t.position))
    fields = [
        _f("state", *state_segs),
        _f("tabs", _t(str(len(s.tabs)))),
        _f("worktrees", _t(str(len(node.worktrees)))),
        _f("agents", _t(str(n_agents), "accent" if n_agents else "dim")),
        _f("layout", _t(layout) if layout else _t("—", "dim"), wrap=True),
    ]
    if not s.inventory_complete:
        fields.append(_f("note", _t("tab/pane inventory incomplete", "warn")))
    return Detail(
        kind="SESSION",
        title=(Seg(s.name, "text", bold=True),),
        fields=tuple(fields),
        actions=(
            DetailAction("enter", "jump"),
            DetailAction("r", "rename", s.state != "exited"),
            DetailAction("x", "kill"),
        ),
    )


def _worktree_detail(wn: WorktreeNode, session: str | None, elapsed: float, home: str | None) -> Detail:
    wt = wn.worktree
    if wn.tabs:
        tab = (_t(f"{session} › {wn.tabs[0].position + 1}"),) if session else (_t(f"tab {wn.tabs[0].position + 1}"),)
        if len(wn.tabs) > 1:
            tab += (_t(f" (+{len(wn.tabs) - 1})", "dim"),)
    else:
        tab = (_t("no tab", "red"),)
    if wt.dirty is None:
        changes = _t("unknown", "dim")
    elif wt.dirty:
        changes = _t(fmt.plural(wt.dirty, "file"), "red")
    else:
        changes = _t("clean", "dim")
    vs: list[Seg] = []
    if wt.ahead is None and wt.behind is None:
        vs.append(_t("unknown", "dim"))
    else:
        if wt.ahead:
            vs.append(_t(f"↑{wt.ahead}", "blue"))
        if wt.behind:
            vs.append(_t(("" if not vs else " ") + f"↓{wt.behind}", "blue"))
        if not vs:
            vs.append(_t("up to date", "dim"))
        if wt.base_ref:
            vs.append(_t(f"  {wt.base_ref}", "dim"))
    branch = _t(wt.branch) if wt.branch else _t(f"detached at {(wt.head or '?')[:7]}", "dim")
    if wt.last_commit:
        last = [_t(wt.last_commit)]
        if wt.last_commit_age_s is not None:
            last.append(_t(f" · {fmt.age(wt.last_commit_age_s + elapsed)}", "dim"))
    else:
        last = [_t("—", "dim")]
    fields = [
        _f("path", _t(fmt.home_short(wt.path, home))),
        _f("branch", branch),
        _f("tab", *tab),
        _f("changes", changes),
        _f(_vs_label(wt.base_ref), *vs),
        _f("agents", _t(str(len(wn.agents)), "accent" if wn.agents else "dim")),
        _f("last commit", *last, wrap=True),
    ]
    flags = [n for n, on in (("root checkout", wt.is_root), ("linked", wn.via_link), ("locked", wt.locked),
                             ("prunable", wt.prunable)) if on]
    if flags:
        fields.append(_f("flags", _t(", ".join(flags), "dim")))
    return Detail(
        kind="WORKTREE",
        title=(Seg(wt.label, "text", bold=True),),
        fields=tuple(fields),
        actions=(
            DetailAction("enter", "jump", bool(session)),
            DetailAction("o", "agent here"),
            DetailAction("t", "open tab", not wn.tabs),
            DetailAction("g", "lazygit"),
            DetailAction("x", "rm…", not wt.is_root),
        ),
    )


def _agent_detail(a: Agent, snapshot: Snapshot, elapsed: float, home: str | None) -> Detail:
    _, wn = find_agent_worktree(snapshot, a.pid)
    if wn is not None:
        where = (_t(wn.worktree.label),)
    elif a.cwd:
        where = (_t(fmt.home_short(a.cwd, home), "dim"),)
    else:
        where = (_t("unknown", "dim"),)
    if a.context_tokens is not None and a.context_limit:
        pct = round(100 * a.context_tokens / a.context_limit)
        ctx = _t(f"{fmt.tokens(a.context_tokens)} / {fmt.tokens(a.context_limit)} ({pct}%)")
    elif a.context_tokens is not None:
        ctx = _t(fmt.tokens(a.context_tokens))
    else:
        ctx = _t("unknown", "dim")
    fields = [
        _f("status", agent_status_seg(a, elapsed)),
        _f("worktree", *where),
        _f("session", _t(a.session_id, "dim") if a.session_id else _t("unknown", "dim")),
        _f("context usage", ctx),
        _f("pid", _t(str(a.pid), "dim")),
        _f("last message", _t(a.last_message) if a.last_message else _t("—", "dim"), wrap=True),
    ]
    if a.status_quality != "exact" and a.status_note:
        fields.append(_f("note", _t(a.status_note, "dim"), wrap=True))
    return Detail(
        kind="AGENT",
        title=(Seg(a.title, "text", bold=True),) if a.title else (Seg("untitled", "dim"),),
        fields=tuple(fields),
        actions=(
            DetailAction("enter", "jump", a.pane_id is not None and a.session_name is not None),
            DetailAction("x", "stop"),
        ),
    )


def _pane_detail(p: ZPane, session: str | None, snapshot: Snapshot, home: str | None) -> Detail:
    wn = find_pane_worktree(snapshot, session, p)
    cmd = " ".join(p.argv) if p.argv else (p.command or p.title or p.zid)
    fields = [
        _f("command", _t(cmd)),
        _f("worktree", _t(wn.worktree.label) if wn else _t("none", "dim")),
        _f("session", _t(session) if session else _t("—", "dim")),
    ]
    if p.cwd:
        fields.append(_f("cwd", _t(fmt.home_short(p.cwd, home), "dim")))
    return Detail(
        kind="PANE",
        title=(Seg(p.command or p.title or p.zid, "text", bold=True),),
        fields=tuple(fields),
        actions=(DetailAction("enter", "focus"),),
    )


def _group_detail(o: Orphans) -> Detail:
    return Detail(
        kind="NEEDS A HOME",
        title=(Seg("Needs a home", "warn", bold=True),),
        fields=(
            _f("worktrees", _t(str(len(o.worktrees)))),
            _f("agents", _t(str(len(o.agents)))),
            _f("summary", _t("Worktrees with no session for their repo, and agents running outside zellij.", "dim"),
               wrap=True),
        ),
    )


def detail_for(row: Row, snapshot: Snapshot, now: float, *, home: str | None = None) -> Detail:
    elapsed = _elapsed(snapshot, now)
    ref = row.ref
    if row.kind == "session" and isinstance(ref, SessionNode):
        return _session_detail(ref, elapsed)
    if row.kind == "worktree" and isinstance(ref, WorktreeNode):
        return _worktree_detail(ref, row.session, elapsed, home)
    if row.kind == "agent" and isinstance(ref, Agent):
        return _agent_detail(ref, snapshot, elapsed, home)
    if row.kind == "pane" and isinstance(ref, ZPane):
        return _pane_detail(ref, row.session, snapshot, home)
    if row.kind == "group" and isinstance(ref, Orphans):
        return _group_detail(ref)
    if row.kind == "warning" and isinstance(ref, SourceWarning):
        return Detail(kind="WARNING", title=(Seg(ref.source, "warn", bold=True),),
                      fields=(_f("message", _t(ref.message), wrap=True),))
    text = "".join(s.text for s in row.label)
    return Detail(kind=row.kind.upper(), title=(Seg(text, "dim"),))
