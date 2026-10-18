"""Snapshot + UiState -> View (flat list of Rows), plus fold/selection helpers. Pure.

Seg conventions for the UI: `label` segs are concatenated as-is; `meta` segs are separate
tokens the UI joins with a single space.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from agentree.types import (
    WORKTREE_MODES,
    Agent,
    Orphans,
    Row,
    Seg,
    SessionNode,
    Snapshot,
    UiState,
    View,
    Worktree,
    WorktreeNode,
    ZPane,
)

from . import fmt
from .filters import FWorktree, apply_filters

AGENT_ROLE = {"busy": "accent", "idle": "green", "needs_input": "red", "done": "dim", "unknown": "dim"}
STATE_ICON = {"attached": Seg("●", "green"), "detached": Seg("○", "dim"), "exited": Seg("✕", "red")}
GROUP_KEY = "g:orphans"
FOOTER_KEY = "footer"


# --------------------------------------------------------------------------- keys

def session_key(name: str) -> str:
    return f"s:{name}"


def worktree_key(session: str, path: str) -> str:
    return f"w:{session}:{path}"


def orphan_worktree_key(path: str) -> str:
    return f"ow:{path}"


def agent_key(pid: int) -> str:
    return f"a:{pid}"


def pane_key(session: str, pane: ZPane) -> str:
    return f"p:{session}:{pane.zid}"


# --------------------------------------------------------------------------- segs

def agent_status_seg(agent: Agent, elapsed: float = 0.0) -> Seg:
    since = None if agent.status_since_s is None else agent.status_since_s + elapsed
    st = agent.status
    if st == "busy":
        text = "busy" if since is None else f"busy {fmt.age(since)}"
    elif st == "idle":
        text = "idle"
    elif st == "needs_input":
        text = "needs input"
    elif st == "done":
        text = "done" if since is None else f"done {fmt.age(since)}"
    else:
        text = "unknown"
    if agent.pane_id is None:
        word = "needs input" if st == "needs_input" else st
        return Seg(f"{word} · no pane", "red")
    return Seg(text, AGENT_ROLE.get(st, "dim"))


def session_state_seg(s, elapsed: float = 0.0) -> Seg:
    a = None if s.created_age_s is None else s.created_age_s + elapsed
    if s.state == "attached":
        return Seg("attached", "green")
    suffix = "" if a is None else f" {fmt.age(a)}"
    if s.state == "detached":
        return Seg(f"detached{suffix}", "dim")
    return Seg(f"exited{suffix}", "red")


def session_agent_count(node: SessionNode) -> int:
    return sum(len(w.agents) for w in node.worktrees) + len(node.loose_agents)


def worktree_change_segs(wt: Worktree) -> list[Seg]:
    segs: list[Seg] = []
    if wt.dirty is not None:
        segs.append(Seg(f"±{wt.dirty}", "red") if wt.dirty > 0 else Seg("clean", "dim"))
    if wt.ahead:
        segs.append(Seg(f"↑{wt.ahead}", "blue"))
    if wt.behind:
        segs.append(Seg(f"↓{wt.behind}", "blue"))
    return segs


def _session_row_parts(node: SessionNode, elapsed: float):
    s = node.session
    meta: list[Seg] = []
    if s.state != "exited":
        meta.append(Seg(fmt.plural(len(s.tabs), "tab"), "dim"))
    n = session_agent_count(node)
    if n:
        meta.append(Seg(f"◆{n}", "accent"))
    meta.append(session_state_seg(s, elapsed))
    return STATE_ICON.get(s.state, Seg("?", "dim")), (Seg(s.name, "text", bold=True),), tuple(meta)


def _worktree_row_parts(wt: Worktree, tabs: tuple, orphan: bool = False):
    icon = Seg("⎇", "dim" if wt.is_root else "teal")
    label = (Seg(wt.label, "text"),)
    meta: list[Seg] = []
    if wt.is_root:
        meta.append(Seg("root", "dim"))
    if tabs:
        meta.append(Seg(f"tab {tabs[0].position + 1}", "dim"))
    else:
        meta.append(Seg("no tab", "red"))
    meta.extend(worktree_change_segs(wt))
    return icon, label, tuple(meta)


def _agent_row_parts(agent: Agent, elapsed: float, branch: str | None = None):
    icon = Seg("◆", AGENT_ROLE.get(agent.status, "dim"))
    label = (Seg(agent.title, "text"),) if agent.title else (Seg("untitled", "dim"),)
    meta = [agent_status_seg(agent, elapsed)]
    if branch is not None:
        meta.append(Seg(f"⎇ {branch}", "teal"))
    return icon, label, tuple(meta)


def pane_label(pane: ZPane, home: str | None = None) -> tuple[Seg, ...]:
    cmd = pane.command or pane.title or pane.zid
    rest = ""
    if len(pane.argv) > 1:
        rest = " ".join(pane.argv[1:])
    elif pane.cwd:
        rest = fmt.home_short(pane.cwd, home)
    segs = [Seg(cmd, "text")]
    if rest:
        segs.append(Seg(f"  {rest}", "dim"))
    return tuple(segs)


def _pane_row_parts(pane: ZPane, home: str | None, branch: str | None = None):
    meta = (Seg(f"⎇ {branch}", "teal"),) if branch is not None else ()
    return Seg("▪", "dim"), pane_label(pane, home), meta


# --------------------------------------------------------------------------- tree assembly

@dataclass
class _N:
    key: str
    kind: str
    icon: Seg | None
    label: tuple
    meta: tuple
    ref: object
    session: str | None
    default_open: bool = False
    children: list["_N"] = field(default_factory=list)


def _fw_children(fw: FWorktree, sname: str, elapsed: float, home: str | None,
                 branch: str | None = None) -> list[_N]:
    out: list[_N] = []
    for a in fw.agents:
        out.append(_N(agent_key(a.pid), "agent", *_agent_row_parts(a, elapsed, branch), ref=a, session=sname))
    for p in fw.panes:
        out.append(_N(pane_key(sname, p), "pane", *_pane_row_parts(p, home, branch), ref=p, session=sname))
    return out


@dataclass(frozen=True, slots=True)
class FooterCounts:
    """What the footer reports. `filters.Filtered` carries the same attributes."""
    hidden_worktrees: int = 0          # by the W mode
    hidden_text_worktrees: int = 0     # by text, inside sessions that stay visible + orphans
    hidden_by_text: int = 0            # whole sessions removed by text
    hidden_detached: int = 0           # by D
    hidden_exited: int = 0             # by D


def footer_parts(counts) -> list[str]:
    """Footer pieces, most important first; hints (`W cycles`, `esc clears`) last.

    The footer row label is `" · ".join(parts)`; the UI drops trailing parts when narrow.
    """
    wt = getattr(counts, "hidden_worktrees", 0)
    twt = getattr(counts, "hidden_text_worktrees", 0)
    ts = getattr(counts, "hidden_by_text", 0)
    det = getattr(counts, "hidden_detached", 0)
    ex = getattr(counts, "hidden_exited", 0)
    parts: list[str] = []
    if wt:
        parts.append(f"{fmt.plural(wt, 'worktree')} hidden by filter")
    if twt:
        # right after the filter part the noun is implied: "3 worktrees hidden by filter · 2 hidden by text"
        parts.append(f"{twt} hidden by text" if wt else f"{fmt.plural(twt, 'worktree')} hidden by text")
    if ts:
        parts.append(f"{fmt.plural(ts, 'session')} hidden by text")
    if det and ex:
        parts.append(f"{det} detached + {ex} exited {'session' if det + ex == 1 else 'sessions'} hidden")
    elif det:
        parts.append(f"{fmt.plural(det, 'detached session')} hidden")
    elif ex:
        parts.append(f"{fmt.plural(ex, 'exited session')} hidden")
    if wt:
        parts.append("W cycles")
    if twt or ts:
        parts.append("esc clears")
    return parts


def footer_text(hidden_worktrees: int = 0, hidden_detached: int = 0, hidden_by_text: int = 0, *,
                hidden_text_worktrees: int = 0, hidden_exited: int = 0) -> str:
    return " · ".join(footer_parts(FooterCounts(
        hidden_worktrees=hidden_worktrees, hidden_text_worktrees=hidden_text_worktrees,
        hidden_by_text=hidden_by_text, hidden_detached=hidden_detached, hidden_exited=hidden_exited)))


def build_view(snapshot: Snapshot, ui: UiState, now: float, *, home: str | None = None) -> View:
    elapsed = max(0.0, now - snapshot.taken_at) if now else 0.0
    f = apply_filters(snapshot, ui)
    text_active = bool(ui.text.strip())
    folds = dict(ui.folds)

    roots: list[_N] = []
    for fs in f.sessions:
        node = fs.node
        sname = node.session.name
        sn = _N(session_key(sname), "session", *_session_row_parts(node, elapsed), ref=node, session=sname,
                default_open=node.session.state == "attached")
        roots.append(sn)
        if node.session.state == "exited":
            continue                       # exited sessions are session-only rows (caret None)
        for fw in fs.worktrees:
            wn = fw.node
            w = _N(worktree_key(sname, wn.worktree.path), "worktree",
                   *_worktree_row_parts(wn.worktree, wn.tabs), ref=wn, session=sname,
                   default_open=bool(wn.agents))
            w.children = _fw_children(fw, sname, elapsed, home)
            sn.children.append(w)
        for fw in fs.reparented:
            sn.children.extend(_fw_children(fw, sname, elapsed, home, branch=fw.node.worktree.label))
        for a in fs.loose_agents:
            sn.children.append(_N(agent_key(a.pid), "agent", *_agent_row_parts(a, elapsed), ref=a, session=sname))
        for p in fs.loose_panes:
            sn.children.append(_N(pane_key(sname, p), "pane", *_pane_row_parts(p, home), ref=p, session=sname))

    if f.orphan_worktrees or f.orphan_agents:
        summary: list[str] = []
        if f.orphan_worktrees:
            summary.append(fmt.plural(len(f.orphan_worktrees), "worktree"))
        if f.orphan_agents:
            summary.append(fmt.plural(len(f.orphan_agents), "agent"))
        g = _N(GROUP_KEY, "group", Seg("⌂", "warn"), (Seg("Needs a home", "warn", bold=True),),
               (Seg(" · ".join(summary), "dim"),),
               ref=Orphans(worktrees=f.orphan_worktrees, agents=f.orphan_agents), session=None,
               default_open=True)
        for wt in f.orphan_worktrees:
            g.children.append(_N(orphan_worktree_key(wt.path), "worktree", *_worktree_row_parts(wt, (), True),
                                 ref=WorktreeNode(worktree=wt), session=None))
        for a in f.orphan_agents:
            g.children.append(_N(agent_key(a.pid), "agent", *_agent_row_parts(a, elapsed), ref=a, session=None))
        roots.append(g)

    rows: list[Row] = []
    for i, w in enumerate(snapshot.warnings):
        rows.append(Row(key=f"warn:{i}", kind="warning", depth=0, caret=None, icon=Seg("!", "warn"),
                        label=(Seg(f"{w.source}: {w.message}", "warn"),), meta=(), ref=w,
                        selectable=False))

    def emit(n: _N, depth: int, parent: str | None) -> None:
        if n.children:
            default = True if text_active else n.default_open
            is_open = folds.get(n.key, default)
            caret = "open" if is_open else "closed"
        else:
            is_open, caret = False, None
        rows.append(Row(key=n.key, kind=n.kind, depth=depth, caret=caret, icon=n.icon, label=n.label,
                        meta=n.meta, ref=n.ref, session=n.session, parent=parent))
        if is_open:
            for c in n.children:
                emit(c, depth + 1, n.key)

    for r in roots:
        emit(r, 0, None)

    ft = " · ".join(footer_parts(f))
    if ft:
        rows.append(Row(key=FOOTER_KEY, kind="footer", depth=0, caret=None, icon=None,
                        label=(Seg(ft, "dim"),), meta=(), selectable=False))

    return View(rows=tuple(rows), hidden_worktrees=f.hidden_worktrees, hidden_sessions=f.hidden_sessions,
                hidden_by_text=f.hidden_by_text)


# --------------------------------------------------------------------------- helpers

def _find(view: View, key: str | None) -> int:
    if key is None:
        return -1
    for i, r in enumerate(view.rows):
        if r.key == key:
            return i
    return -1


def row_for(view: View, key: str | None) -> Row | None:
    i = _find(view, key)
    return view.rows[i] if i >= 0 else None


def set_fold(ui: UiState, key: str, expanded: bool) -> UiState:
    folds = tuple((k, v) for k, v in ui.folds if k != key) + ((key, expanded),)
    return replace(ui, folds=folds)


def toggle_fold(ui: UiState, view: View, key: str) -> UiState:
    """Flip the fold of `key`; no-op for rows without children."""
    row = row_for(view, key)
    if row is None or row.caret is None:
        return ui
    return set_fold(ui, key, row.caret != "open")


def select_move(view: View, key: str | None, delta: int) -> str | None:
    """Move selection by `delta` selectable rows (clamped). Unknown key -> first/last selectable."""
    sel = [r.key for r in view.rows if r.selectable]
    if not sel:
        return None
    if key not in sel:
        return sel[0] if delta >= 0 else sel[-1]
    i = sel.index(key) + delta
    return sel[max(0, min(len(sel) - 1, i))]


def resolve_selection(view: View, key: str | None) -> str | None:
    """Keep `key` if it is a selectable visible row, else the first selectable row."""
    r = row_for(view, key)
    if r is not None and r.selectable:
        return key
    return select_move(view, None, 1)


def parent_key(view: View, key: str | None) -> str | None:
    r = row_for(view, key)
    return r.parent if r else None


def first_child_key(view: View, key: str | None) -> str | None:
    i = _find(view, key)
    if i < 0 or i + 1 >= len(view.rows):
        return None
    nxt = view.rows[i + 1]
    return nxt.key if nxt.parent == key else None


def cycle_worktree_mode(ui: UiState) -> UiState:
    i = WORKTREE_MODES.index(ui.worktree_mode) if ui.worktree_mode in WORKTREE_MODES else -1
    return replace(ui, worktree_mode=WORKTREE_MODES[(i + 1) % len(WORKTREE_MODES)])


def toggle(ui: UiState, name: str) -> UiState:
    """Flip a boolean toggle: 'show_agents' | 'show_panes' | 'show_detached'."""
    return replace(ui, **{name: not getattr(ui, name)})


def render_plain(view: View) -> str:
    """Plain-text rendering (debugging / tests): indent 2 per depth, caret, icon, label, meta."""
    out: list[str] = []
    for r in view.rows:
        caret = {"open": "▾", "closed": "▸", None: " "}[r.caret]
        icon = r.icon.text if r.icon else ""
        label = "".join(s.text for s in r.label)
        meta = " ".join(s.text for s in r.meta)
        line = f"{'  ' * r.depth}{caret} {icon} {label}".rstrip()
        out.append(f"{line}    {meta}" if meta else line)
    return "\n".join(out)
