"""Link raw source records into a Snapshot: session > worktree > {agents, panes}.

Pure: no I/O. See `link()` for the rules.
"""
from __future__ import annotations

import posixpath
from typing import Any, Mapping, Sequence

from agentree.types import (
    Agent,
    KittyHost,
    Orphans,
    Repo,
    SessionNode,
    Snapshot,
    SourceWarning,
    Worktree,
    WorktreeNode,
    ZPane,
    ZSession,
    ZTab,
)

from .fmt import norm

_STATE_RANK = {"attached": 0, "detached": 1, "exited": 2}


class WorktreeIndex:
    """Index of all worktrees across repos; resolves the deepest containing worktree."""

    def __init__(self, repos: Sequence[Repo]):
        self.by_path: dict[str, Worktree] = {}
        self.repo_of: dict[str, Repo] = {}
        for repo in repos:
            for wt in repo.worktrees:
                p = norm(wt.path)
                self.by_path[p] = wt
                self.repo_of[p] = repo

    def containing(self, path: str | None) -> Worktree | None:
        """Deepest worktree whose root is a component-wise prefix of `path`."""
        if not path:
            return None
        p = norm(path)
        while True:
            wt = self.by_path.get(p)
            if wt is not None:
                return wt
            parent = posixpath.dirname(p)
            if parent == p or not parent:
                return None
            p = parent


def containing_worktree(path: str | None, repos: Sequence[Repo]) -> Worktree | None:
    return WorktreeIndex(repos).containing(path)


class _SessionBuilder:
    def __init__(self, session: ZSession):
        self.session = session
        self.order: list[str] = []                     # worktree paths present, insertion order
        self.tabs: dict[str, list[ZTab]] = {}
        self.agents: dict[str, list[Agent]] = {}
        self.panes: dict[str, list[tuple[tuple[int, int], ZPane]]] = {}
        self.via_link: set[str] = set()
        self.link_tab: dict[str, ZTab] = {}
        self.loose_agents: list[Agent] = []
        self.loose_panes: list[tuple[tuple[int, int], ZPane]] = []

    def touch(self, path: str) -> None:
        if path not in self.order:
            self.order.append(path)
            self.tabs.setdefault(path, [])
            self.agents.setdefault(path, [])
            self.panes.setdefault(path, [])

    def add_tab(self, path: str, tab: ZTab) -> None:
        self.touch(path)
        if all(t.tab_id != tab.tab_id for t in self.tabs[path]):
            self.tabs[path].append(tab)


def _rec_get(rec: Mapping[str, Any], *names: str) -> Any:
    for n in names:
        if n in rec:
            return rec[n]
    return None


def link(
    sessions: Sequence[ZSession],
    repos: Sequence[Repo],
    agents: Sequence[Agent],
    hosts: Sequence[KittyHost] = (),
    links: Sequence[Mapping[str, Any]] = (),
    warnings: Sequence[SourceWarning] = (),
    now: float = 0.0,
    *,
    orphan_roots: bool = False,
) -> Snapshot:
    """Build the linked snapshot.

    Rules (deepest worktree root wins everywhere):
      * a worktree belongs to a tab when a non-plugin pane cwd in that tab is inside it;
      * agents with session_name + pane_id of a live session go to that session, under the
        worktree containing their cwd (else loose); others are orphans;
      * the pane hosting an agent is not listed as a pane; exited/plugin panes are skipped;
      * exited sessions are session-only rows: they never receive worktrees, panes or agents
        (their tabs, if a resurrect layout lists any, are ignored);
      * links.json records win: worktree placed under record session (via_link), tab named
        tabName attached, listed agent pids attached to it. A record naming an exited session
        does not place anything: the worktree goes to orphans unless a live session has it in
        a tab (the record itself is kept on disk, the session may be resurrected);
      * worktrees with no tab anywhere are homed by pools, first non-empty wins:
          live sessions holding some of the repo's other worktrees > live sessions named after
          the repo (`<repo>` or `<repo>/...`, the spawn convention); within a pool rank by
          (a) most of the repo's worktrees held, (b) named after the repo, (c) attached >
          detached, (d) name. No pool: orphan.
      * root checkouts with no home are dropped unless `orphan_roots` (otherwise every
        unopened repo under the repo roots would land in "Needs a home").
    """
    idx = WorktreeIndex(repos)
    sess_by_name: dict[str, ZSession] = {}
    for s in sessions:
        sess_by_name.setdefault(s.name, s)
    builders = {name: _SessionBuilder(s) for name, s in sess_by_name.items()}

    # ---- links.json records
    link_agent: dict[int, tuple[str, str]] = {}
    linked_to_exited: set[str] = set()
    for rec in links:
        if not isinstance(rec, Mapping):
            continue
        raw_path = _rec_get(rec, "worktreePath", "worktree_path")
        sname = _rec_get(rec, "session")
        if not isinstance(raw_path, str) or not isinstance(sname, str):
            continue
        path = norm(raw_path)
        if path not in idx.by_path or sname not in builders:
            continue
        b = builders[sname]
        if b.session.state == "exited":
            linked_to_exited.add(path)
            continue
        b.touch(path)
        b.via_link.add(path)
        tab_name = _rec_get(rec, "tabName", "tab_name")
        if isinstance(tab_name, str):
            for tab in b.session.tabs:
                if tab.name == tab_name:
                    b.add_tab(path, tab)
                    b.link_tab[path] = tab
                    break
        for pid in _rec_get(rec, "agentPids", "agent_pids") or ():
            try:
                link_agent[int(pid)] = (sname, path)
            except (TypeError, ValueError):
                continue

    # ---- tabs and panes
    agent_panes: dict[str, set[int]] = {}
    for a in agents:
        if a.session_name and a.pane_id is not None:
            agent_panes.setdefault(a.session_name, set()).add(a.pane_id)

    pane_loc: dict[str, dict[int, tuple[ZTab, int]]] = {}
    for name, b in builders.items():
        s = b.session
        locs = pane_loc.setdefault(name, {})
        if s.state == "exited":
            continue
        hosted = agent_panes.get(name, set())
        for tab in sorted(s.tabs, key=lambda t: t.position):
            for i, p in enumerate(tab.panes):
                if p.is_plugin:
                    continue
                locs[p.pane_id] = (tab, i)
                wt = idx.containing(p.cwd)
                if wt is not None:
                    b.add_tab(norm(wt.path), tab)
                if p.pane_id in hosted or p.exited:
                    continue
                order = (tab.position, i)
                if wt is not None:
                    b.panes[norm(wt.path)].append((order, p))
                else:
                    b.loose_panes.append((order, p))

    # ---- agents
    orphan_agents: list[Agent] = []
    for a in agents:
        if a.pid in link_agent:
            sname, path = link_agent[a.pid]
            b = builders[sname]
            b.touch(path)
            b.agents[path].append(a)
            continue
        s = sess_by_name.get(a.session_name) if a.session_name else None
        if s is None or s.state == "exited" or a.pane_id is None:
            orphan_agents.append(a)
            continue
        b = builders[s.name]
        wt = idx.containing(a.cwd)
        if wt is None:
            b.loose_agents.append(a)
            continue
        path = norm(wt.path)
        b.touch(path)
        b.agents[path].append(a)
        if not b.tabs[path]:
            loc = pane_loc.get(s.name, {}).get(a.pane_id)
            if loc is not None:
                b.add_tab(path, loc[0])

    # ---- homing of worktrees that are nowhere yet
    placed: set[str] = set()
    for b in builders.values():
        placed.update(b.order)
    # counts of each repo's worktrees per session, before homing (order independent)
    repo_counts: dict[str, dict[str, int]] = {}
    for name, b in builders.items():
        for path in b.order:
            root = norm(idx.repo_of[path].root)
            c = repo_counts.setdefault(root, {})
            c[name] = c.get(name, 0) + 1

    def rank(name: str) -> tuple[int, str]:
        return (_STATE_RANK.get(sess_by_name[name].state, 3), name)

    def named_after(name: str, repo: Repo) -> bool:
        return name == repo.name or name.startswith(repo.name + "/")

    def live(names) -> list[str]:
        return [n for n in names if sess_by_name[n].state != "exited"]

    orphan_wts: list[Worktree] = []
    for repo in repos:
        root = norm(repo.root)
        counts = repo_counts.get(root, {})

        def key(n: str) -> tuple:
            return (-counts.get(n, 0), not named_after(n, repo), *rank(n))

        holders = [n for n, c in counts.items() if c > 0]
        named = [n for n in sess_by_name if named_after(n, repo)]
        home: str | None = None
        for pool in (live(holders), live(named)):
            if pool:
                home = min(pool, key=key)
                break
        for wt in repo.worktrees:
            path = norm(wt.path)
            if path in placed:
                continue
            if home is not None and path not in linked_to_exited:
                builders[home].touch(path)
            elif orphan_roots or not wt.is_root:
                orphan_wts.append(wt)
            placed.add(path)

    # ---- kitty
    kitty: dict[str, KittyHost] = {}
    for h in hosts:
        cur = kitty.get(h.session)
        if cur is None or (h.focused and not cur.focused):
            kitty[h.session] = h

    # ---- assemble
    nodes: list[SessionNode] = []
    for name, b in builders.items():
        locs = pane_loc.get(name, {})

        def agent_key(a: Agent) -> tuple:
            loc = locs.get(a.pane_id) if a.pane_id is not None else None
            if loc is None:
                return (1, 0, 0, a.pid)
            return (0, loc[0].position, loc[1], a.pid)

        wnodes: list[WorktreeNode] = []
        for path in b.order:
            wt = idx.by_path[path]
            tabs = sorted(b.tabs[path], key=lambda t: t.position)
            lt = b.link_tab.get(path)
            if lt is not None:
                tabs = [lt] + [t for t in tabs if t.tab_id != lt.tab_id]
            wnodes.append(
                WorktreeNode(
                    worktree=wt,
                    tabs=tuple(tabs),
                    agents=tuple(sorted(b.agents[path], key=agent_key)),
                    panes=tuple(p for _, p in sorted(b.panes[path], key=lambda x: x[0])),
                    via_link=path in b.via_link,
                )
            )
        wnodes.sort(
            key=lambda n: (
                0 if n.worktree.is_root else 1,
                0 if n.tabs else 1,
                min((t.position for t in n.tabs), default=0),
                n.worktree.label,
            )
        )
        nodes.append(
            SessionNode(
                session=b.session,
                worktrees=tuple(wnodes),
                loose_agents=tuple(sorted(b.loose_agents, key=agent_key)),
                loose_panes=tuple(p for _, p in sorted(b.loose_panes, key=lambda x: x[0])),
                kitty=kitty.get(name),
            )
        )
    nodes.sort(key=lambda n: (not n.session.is_current, *rank(n.session.name)))

    return Snapshot(
        taken_at=now,
        sessions=tuple(nodes),
        orphans=Orphans(
            worktrees=tuple(sorted(orphan_wts, key=lambda w: (norm(w.repo_root), not w.is_root, w.label))),
            agents=tuple(sorted(orphan_agents, key=lambda a: a.pid)),
        ),
        warnings=tuple(warnings),
    )
