"""Sample data mirroring the brief's tree. Used by tests and `agentree --sample` style demos."""
from __future__ import annotations

import hashlib
from typing import Any

import dataclasses

from agentree.types import Agent, KittyHost, Repo, Snapshot, SourceWarning, Worktree, ZPane, ZSession, ZTab

from .link import link

HOME = "/Users/me"
PROJECTS = f"{HOME}/Projects"
WEB = f"{PROJECTS}/web-app"
WEB_WT = f"{WEB}/.worktrees"
DOT = f"{PROJECTS}/dotfiles"
ENGINE = f"{PROJECTS}/engine"
API = f"{PROJECTS}/api"

H = 3600
D = 86400


def _wt(path: str, root: str, branch: str | None, *, is_root: bool = False, dirty: int = 0,
        ahead: int = 0, behind: int = 0, subject: str = "", age_s: int = H, head: str = "") -> Worktree:
    return Worktree(
        path=path,
        repo_root=root,
        branch=branch,
        head=head or hashlib.sha1(path.encode()).hexdigest(),
        is_root=is_root,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        base_ref="origin/main",
        last_commit=subject or None,
        last_commit_age_s=age_s,
    )


def _pane(pid: int, tab_id: int, cmd: str | None, cwd: str | None, *argv: str, title: str = "",
          plugin: bool = False, exited: bool = False, focused: bool = False) -> ZPane:
    return ZPane(
        pane_id=pid,
        is_plugin=plugin,
        tab_id=tab_id,
        title=title or (cmd or ""),
        command=cmd,
        argv=tuple(argv) if argv else ((cmd,) if cmd else ()),
        cwd=cwd,
        focused=focused,
        exited=exited,
    )


def _with_demo_agents(sessions: tuple[ZSession, ...], now: float, paths: dict[str, str],
                      providers=None) -> tuple[tuple[ZSession, ...], tuple[Agent, ...]]:
    """Every provider's demo agents (sorted by pid) and the sessions with their agent panes added
    to the named tabs (tab panes then ordered by pane id, plugins last)."""
    from agentree.providers import registry
    from agentree.providers.base import DemoContext

    ctx = DemoContext(now=now, paths=paths)
    demos = [d for p in (registry.all_providers() if providers is None else providers)
             for d in p.demo_agents(ctx)]
    extra: dict[tuple[str, int], list[ZPane]] = {}
    for d in demos:
        if d.pane is not None and d.session_name is not None and d.tab_id is not None:
            extra.setdefault((d.session_name, d.tab_id), []).append(d.pane)

    def tab(s: ZSession, t: ZTab) -> ZTab:
        add = extra.get((s.name, t.tab_id))
        if not add:
            return t
        panes = sorted((*t.panes, *add), key=lambda p: (p.is_plugin, p.pane_id))
        return dataclasses.replace(t, panes=tuple(panes))

    out = tuple(dataclasses.replace(s, tabs=tuple(tab(s, t) for t in s.tabs)) for s in sessions)
    return out, tuple(sorted((d.agent for d in demos), key=lambda a: a.pid))


def sample_raw(now: float) -> dict[str, Any]:
    """Raw source records (kwargs for `link()` minus `now`)."""
    repos = (
        Repo(WEB, "web-app", "origin/main", (
            _wt(WEB, WEB, "main", is_root=True, subject="Merge pull request #212", age_s=2 * H,
                head="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"),
            _wt(f"{WEB_WT}/sso-login", WEB, "feat/sso-login", dirty=6, ahead=3,
                subject="Extract token refresh into hook", age_s=25 * 60),
            _wt(f"{WEB_WT}/flaky-e2e", WEB, "fix/flaky-e2e", dirty=2, ahead=1,
                subject="Retry checkout spec on network idle", age_s=3 * H),
            _wt(f"{WEB_WT}/billing-export", WEB, "feat/billing-export", behind=4,
                subject="Add CSV export endpoint", age_s=2 * D),
            _wt(f"{WEB_WT}/session-store", WEB, "chore/session-store", dirty=3, ahead=1,
                subject="Move session store to redis", age_s=6 * H),
        )),
        Repo(DOT, "dotfiles", "origin/main", (
            _wt(DOT, DOT, "main", is_root=True, dirty=1, subject="fish: add za helper", age_s=D),
        )),
        Repo(ENGINE, "engine", "origin/main", (
            _wt(ENGINE, ENGINE, "main", is_root=True, subject="Bump version to 0.9.2", age_s=4 * D),
            _wt(f"{ENGINE}/.worktrees/simd-hash", ENGINE, "perf/simd-hash", dirty=4, ahead=2,
                subject="SIMD probe for hash table", age_s=5 * H),
        )),
        Repo(API, "api", "origin/main", (
            _wt(API, API, "main", is_root=True, subject="Initial commit", age_s=30 * D),
            _wt(f"{API}/.worktrees/rate-limit", API, "agent/rate-limit", dirty=2, ahead=5,
                subject="Token bucket limiter", age_s=40 * 60),
        )),
    )

    sso = f"{WEB_WT}/sso-login"
    flaky = f"{WEB_WT}/flaky-e2e"
    sessions = (
        ZSession(
            name="web-app", state="attached", created_age_s=26 * H, socket_dir="/tmp/zellij-502",
            clients=1, is_current=True,
            tabs=(
                ZTab(0, 0, "web-app", True, (
                    _pane(0, 0, "nvim", WEB, "nvim", "src/app.tsx", focused=True),
                    _pane(1, 0, "fish", WEB),
                    _pane(7, 0, "htop", HOME),
                    _pane(0, 0, None, None, title="tab-bar", plugin=True),
                )),
                ZTab(1, 1, "sso-login", False, (
                    _pane(4, 1, "fish", f"{sso}/src/auth"),
                )),
                ZTab(2, 2, "flaky-e2e", False, (
                    _pane(5, 2, "nvim", flaky, "nvim", "e2e/checkout.spec.ts"),
                    _pane(6, 2, "pnpm", flaky, "pnpm", "test", exited=True),
                )),
            ),
        ),
        ZSession(
            name="dotfiles", state="attached", created_age_s=4 * D, socket_dir="/tmp/zellij-502",
            clients=1,
            tabs=(
                ZTab(10, 0, "dotfiles", True, (
                    _pane(0, 10, "nvim", DOT, "nvim", "fish/config.fish"),
                )),
            ),
        ),
        ZSession(
            name="engine", state="detached", created_age_s=5 * H, socket_dir="/tmp/zellij-502",
            clients=0,
            tabs=(
                ZTab(20, 0, "engine", True, (_pane(0, 20, "fish", ENGINE),)),
                ZTab(21, 1, "simd-hash", False, (
                    _pane(1, 21, "nvim", f"{ENGINE}/.worktrees/simd-hash", "nvim", "src/hash.rs"),
                )),
            ),
        ),
        ZSession(name="scratch", state="exited", created_age_s=3 * D, socket_dir=None),
    )

    sessions, agents = _with_demo_agents(sessions, now, {
        "home": HOME, "web": WEB, "sso": sso, "flaky": flaky, "dotfiles": DOT, "engine": ENGINE,
        "simd": f"{ENGINE}/.worktrees/simd-hash", "api": API,
        "rate_limit": f"{API}/.worktrees/rate-limit",
    })

    hosts = (
        KittyHost("unix:/tmp/kitty.sock-11854", 1, 1, 1, "web-app", focused=True),
        KittyHost("unix:/tmp/kitty.sock-11854", 1, 2, 2, "dotfiles"),
    )
    links: tuple[dict, ...] = ()
    warnings: tuple[SourceWarning, ...] = ()
    return {
        "sessions": sessions,
        "repos": repos,
        "agents": agents,
        "hosts": hosts,
        "links": links,
        "warnings": warnings,
    }


def sample_snapshot(now: float) -> Snapshot:
    return link(**sample_raw(now), now=now)
