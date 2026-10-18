"""Pure filtering of a Snapshot by UiState (worktree mode, toggles, text).

Produces an intermediate `Filtered` structure plus hidden counts; tree.py turns it into rows.
"""
from __future__ import annotations

from dataclasses import dataclass

from agentree.types import (
    Agent,
    SessionNode,
    Snapshot,
    UiState,
    Worktree,
    WorktreeMode,
    WorktreeNode,
    ZPane,
)


@dataclass(frozen=True, slots=True)
class FWorktree:
    node: WorktreeNode
    agents: tuple[Agent, ...]
    panes: tuple[ZPane, ...]


@dataclass(frozen=True, slots=True)
class FSession:
    node: SessionNode
    worktrees: tuple[FWorktree, ...]        # visible worktree rows
    reparented: tuple[FWorktree, ...]       # hide mode: worktrees whose children attach to the session
    loose_agents: tuple[Agent, ...]
    loose_panes: tuple[ZPane, ...]
    text_active: bool = False


@dataclass(frozen=True, slots=True)
class Filtered:
    sessions: tuple[FSession, ...]
    orphan_worktrees: tuple[Worktree, ...]
    orphan_agents: tuple[Agent, ...]
    hidden_worktrees: int = 0          # worktree rows hidden by the W mode
    hidden_sessions: int = 0           # sessions hidden by D (= hidden_detached + hidden_exited)
    hidden_by_text: int = 0            # sessions removed entirely by the text filter
    hidden_text_worktrees: int = 0     # worktrees removed by text in still-visible sessions + orphans
    hidden_detached: int = 0
    hidden_exited: int = 0


def norm_query(text: str) -> str:
    return text.strip().casefold()


def matches(query: str, value: str | None) -> bool:
    """Case-insensitive substring match; empty query matches everything."""
    if not query:
        return True
    return bool(value) and query in value.casefold()


def worktree_passes(wt: Worktree, has_tab: bool, mode: WorktreeMode) -> bool:
    """Whether a worktree row is visible in `mode` (hide never shows worktree rows)."""
    if mode == "all":
        return True
    if mode == "with_tab":
        return has_tab
    if mode == "dirty":
        return (wt.dirty or 0) > 0
    return False


def session_visible(node: SessionNode, ui: UiState) -> bool:
    return ui.show_detached or node.session.state == "attached"


def apply_filters(snapshot: Snapshot, ui: UiState) -> Filtered:
    q = norm_query(ui.text)
    hidden_wt = hidden_text = hidden_text_wt = hidden_det = hidden_exit = 0
    out: list[FSession] = []

    def strip(fw: FWorktree) -> FWorktree:
        return FWorktree(
            fw.node,
            fw.agents if ui.show_agents else (),
            fw.panes if ui.show_panes else (),
        )

    for node in snapshot.sessions:
        if not session_visible(node, ui):
            if node.session.state == "exited":
                hidden_exit += 1
            else:
                hidden_det += 1
            continue
        smatch = matches(q, node.session.name)
        kept: list[FWorktree] = []
        for wn in node.worktrees:
            if smatch:
                kept.append(FWorktree(wn, wn.agents, wn.panes))
                continue
            if matches(q, wn.worktree.label):
                kept.append(FWorktree(wn, wn.agents, wn.panes))
                continue
            am = tuple(a for a in wn.agents if matches(q, a.title))
            if am:
                kept.append(FWorktree(wn, am, ()))
        loose_agents = node.loose_agents if smatch else tuple(
            a for a in node.loose_agents if matches(q, a.title))
        loose_panes = node.loose_panes if smatch else ()
        if not smatch and not kept and not loose_agents:
            hidden_text += 1               # its worktrees are covered by the session count
            continue
        hidden_text_wt += len(node.worktrees) - len(kept)

        visible: list[FWorktree] = []
        reparented: list[FWorktree] = []
        for fw in kept:
            if ui.worktree_mode == "hide":
                hidden_wt += 1
                reparented.append(strip(fw))
            elif worktree_passes(fw.node.worktree, bool(fw.node.tabs), ui.worktree_mode):
                visible.append(strip(fw))
            else:
                hidden_wt += 1
        out.append(FSession(
            node=node,
            worktrees=tuple(visible),
            reparented=tuple(reparented),
            loose_agents=loose_agents if ui.show_agents else (),
            loose_panes=loose_panes if ui.show_panes else (),
            text_active=bool(q),
        ))

    owts: list[Worktree] = []
    for wt in snapshot.orphans.worktrees:
        if not matches(q, wt.label):
            hidden_text_wt += 1
            continue
        if worktree_passes(wt, False, ui.worktree_mode):
            owts.append(wt)
        else:
            hidden_wt += 1
    oagents = tuple(a for a in snapshot.orphans.agents if matches(q, a.title)) if ui.show_agents else ()

    return Filtered(
        sessions=tuple(out),
        orphan_worktrees=tuple(owts),
        orphan_agents=oagents,
        hidden_worktrees=hidden_wt,
        hidden_sessions=hidden_det + hidden_exit,
        hidden_by_text=hidden_text,
        hidden_text_worktrees=hidden_text_wt,
        hidden_detached=hidden_det,
        hidden_exited=hidden_exit,
    )
