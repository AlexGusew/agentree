"""The real Actions implementation used by the TUI (M2 jump/open tab, M3 spawn/remove/rename,
M4 new agent `o` / lazygit `g`).

Kept import-light: jump/tabs/exec (subprocess) load on first use, not at startup.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from agentree.actions.api import ActionResult, AgentOptions, Plan, SpawnOptions


class RealActions:
    def __init__(self, cfg: Any, dry_run: bool = False, *, run_fn: Callable[..., Any] | None = None,
                 environ: Mapping[str, str] | None = None, sockets: Callable[[], list[str]] | None = None,
                 sleep: Callable[[float], None] | None = None, state_dir: str | None = None,
                 nav_overrides: Mapping[str, Any] | None = None, stream_fn: Callable[..., Any] | None = None,
                 identity: Callable[[int], Any] | None = None,
                 find_agents: Callable[[str, set], list] | None = None, links: Any = None,
                 journal_store: Any = None) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self._run_fn = run_fn
        self._environ = environ
        self._sockets = sockets
        self._sleep = sleep
        self._state_dir = state_dir
        self._nav_overrides = dict(nav_overrides or {})
        self._stream_fn = stream_fn
        self._identity = identity            # pid -> {start, argv, env, cwd} (tests)
        self._find_agents = find_agents      # (session, pane ids) -> agent pids (tests)
        self._links = links                  # links.json module (tests); default agentree.state
        self._own_rename: str | None = None
        self._journal_store = journal_store      # journal persistence (tests); default agentree.state   # agentree's own session was renamed to this (until exit)

    # ------------------------------------------------------------------ plumbing
    def navigator(self):
        """A fresh Navigator (and Runner) per action, so dry-run counts are per action."""
        from agentree.actions import exec as ex
        from agentree.actions.jump import Env, Navigator
        from agentree.actions.runner import Runner
        from agentree.sources import kitty as ksrc

        from agentree.sources import zellij as zsrc

        glob_ = getattr(self.cfg, "kitty_socket_glob", None)
        cfg_dirs = list(getattr(self.cfg, "zellij_socket_dirs", ()) or ())
        env = Env.from_environ(self._environ)
        if self._own_rename and env.current_session:
            import dataclasses
            # ZELLIJ_SESSION_NAME is stale after renaming our own session: keep refusing it
            env = dataclasses.replace(env, current_session=self._own_rename)

        def socket_dirs() -> list[str]:
            dirs = zsrc.socket_dirs(cfg_dirs or None)
            # the session agentree runs in is always considered, even with an explicit config
            if env.current_session and env.socket_dir and env.socket_dir not in dirs:
                dirs.append(env.socket_dir)
            return dirs

        nav = Navigator(
            runner=Runner(self.dry_run, self._run_fn, self._stream_fn),
            env=env,
            zellij=ex.which("zellij") or "zellij",
            kitty=ksrc.kitty_bin(),
            sockets=self._sockets or (lambda: ksrc.sockets(glob_)),
            socket_dirs=socket_dirs,
            owned=ksrc.is_owned_socket,
        )
        if self._sleep is not None:
            nav.sleep = self._sleep
        for k, v in self._nav_overrides.items():
            setattr(nav, k, v)
        return nav

    def state_dir(self) -> str:
        if self._state_dir:
            return self._state_dir
        from agentree.actions import exec as ex
        return ex.state_dir()

    # ------------------------------------------------------------------ M2
    def jump(self, row, snap) -> ActionResult:
        from agentree.actions.jump import resolve

        t = resolve(row, snap)
        if isinstance(t, str):
            return ActionResult(False, t)
        return self.navigator().go(t)

    def open_tab(self, row, snap) -> ActionResult:
        from agentree.actions.tabs import open_tab

        return open_tab(self.navigator(), row, snap, state_dir=self.state_dir(),
                        layout=getattr(self.cfg, "tab_layout", None))

    # ------------------------------------------------------------------ M3
    def rename_session(self, row, snap, new_name) -> ActionResult:
        from agentree.actions.rename import rename_session

        nav = self.navigator()
        res = rename_session(row, snap, new_name, nav, links=self._links)
        if res.ok and getattr(nav, "renamed_own", None):
            self._own_rename = nav.renamed_own
        return res

    def plan_remove(self, row, snap):
        from agentree.actions.remove import plan_remove

        return plan_remove(row, snap, nav=self.navigator(), identity=self._identity)

    def spawn_options(self, row, snap) -> SpawnOptions:
        from agentree.actions.spawn import spawn_options

        return spawn_options(row, snap, self.cfg)

    def plan_spawn(self, req, snap) -> Plan:
        from agentree.actions.spawn import plan_spawn

        return plan_spawn(req, snap, cfg=self.cfg, nav=self.navigator(), state_dir=self.state_dir())

    def execute(self, plan, typed=None, *, progress=None, cancel=None) -> ActionResult:
        if plan.errors:
            return ActionResult(False, plan.errors[0])
        if plan.kind == "spawn":
            from agentree.actions.spawn import execute_spawn

            return execute_spawn(plan, self.navigator(), cfg=self.cfg, state_dir=self.state_dir(),
                                 progress=progress, cancel=cancel, find_agents=self._find_agents,
                                 links=self._links, journal_store=self._journal_store)
        if plan.kind in ("remove-worktree", "kill-agent", "kill-session"):
            from agentree.actions.remove import execute_remove

            return execute_remove(plan, typed, self.navigator(), identity=self._identity, progress=progress,
                                  cancel=cancel, links=self._links, state_dir=self.state_dir())
        return ActionResult(False, f"{plan.kind}: unknown action")

    # ------------------------------------------------------------------ M4
    def agent_options(self) -> AgentOptions:
        """Every registered agent provider with its availability (its launch command resolves),
        and the default (registry.default_id(cfg)). Computed here, on the action worker."""
        from agentree.actions.agent import agent_options

        return agent_options(self.cfg)

    def new_agent(self, row, snap, provider=None) -> ActionResult:
        from agentree.actions.agent import new_agent

        return new_agent(self.navigator(), row, snap, cfg=self.cfg, state_dir=self.state_dir(),
                         links=self._links, find_agents=self._find_agents, provider=provider)

    def lazygit(self, row, snap) -> ActionResult:
        from agentree.actions.lazygit import lazygit

        return lazygit(self.navigator(), row, snap, cfg=self.cfg, state_dir=self.state_dir())
