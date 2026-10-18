"""Action interface between the UI and the side-effecting action layer.

The UI only calls these methods; it never shells out itself.

Threading contract (M2 gate finding 9):
- The UI runs every Actions method on ONE owned action worker thread, never on the
  terminal event loop. At most one action runs at a time; results and ProgressEvents are
  marshalled back to the UI loop through its queue. Action code must never touch UI state.
- `plan_spawn` and `spawn_options` are cheap, side-effect free (may read the filesystem and
  run read-only git commands) and may be called on every form edit to refresh the preview.
- `execute` reports progress through `progress` and checks `cancel` between steps. Once a
  step that creates something has started, cancellation means "stop and roll back what was
  created", never "abandon silently".
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from agentree.types import Row, Snapshot


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    message: str = ""
    close: bool = False          # UI should exit (e.g. after a successful jump)
    refresh: bool = False        # UI should refresh the snapshot


@dataclass(frozen=True, slots=True)
class Confirm:
    """A destructive step the UI must confirm before calling Actions.execute(plan)."""
    title: str
    lines: tuple[str, ...] = ()          # what will happen, incl. exact commands
    require_text: str | None = None      # user must type this exactly (e.g. branch name when dirty)
    danger: bool = True


@dataclass(frozen=True, slots=True)
class Plan:
    """A prepared action: commands to run once confirmed."""
    kind: str                            # "remove-worktree" | "kill-agent" | "kill-session" | "spawn" | ...
    confirm: Confirm | None
    steps: tuple[tuple[str, ...], ...] = ()   # display lines (argv joined or prose), shown as preview
    data: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()       # non-blocking issues shown in the preview
    errors: tuple[str, ...] = ()         # blocking issues: UI must not allow executing


StepState = Literal["pending", "running", "ok", "failed", "skipped", "rolled_back"]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    index: int                           # 0-based step index into Plan.steps
    total: int
    label: str
    state: StepState
    detail: str = ""                     # last stderr line, rollback note, ...


Progress = Callable[[ProgressEvent], None]


# --------------------------------------------------------------------------- spawn (M3)

# --------------------------------------------------------------------------- agents

@dataclass(frozen=True, slots=True)
class AgentChoice:
    """One registered agent provider, as offered by `o` and the spawn form."""
    id: str                              # provider id (registry)
    label: str                           # user-facing name
    available: bool                      # its launch command resolves (computed on the action worker)


@dataclass(frozen=True, slots=True)
class AgentOptions:
    """Agent choices for `o` (agent here), in registry order."""
    choices: tuple[AgentChoice, ...]
    default_provider: str                # registry.default_id(cfg)


@dataclass(frozen=True, slots=True)
class SpawnOptions:
    """Choices for the spawn form, derived from config + snapshot."""
    repos: tuple[str, ...]               # repo root paths
    default_repo: str | None             # repo of the selected row, if any
    layouts: tuple[str, ...]             # zellij layout names from config
    bases: tuple[str, ...] = ("origin/main", "HEAD")   # display; plan resolves origin default branch
    targets: tuple[str, ...] = ("tab", "session")      # new tab in repo's session | new session <repo>-<slug>
    branch_prefix: str = ""              # prefill for the branch field (cfg.branch_prefix, e.g. "agent/")
    agents: tuple[AgentChoice, ...] = ()  # agent providers the agent layout / prompt can start
    default_provider: str = "opencode"   # preselected agent (registry.default_id(cfg))


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    repo: str                            # repo root path
    branch: str
    base: Literal["origin", "head"] = "origin"
    target: Literal["tab", "session"] = "tab"
    layout: str | None = None
    prompt: str = ""
    copy_env: bool = True
    run_install: bool = False
    switch_after: bool = True
    agent: str = "opencode"              # provider id started by a prompt / the "agent" layout


class Actions(Protocol):
    dry_run: bool

    def jump(self, row: Row, snap: Snapshot) -> ActionResult: ...
    def open_tab(self, row: Row, snap: Snapshot) -> ActionResult: ...
    def agent_options(self) -> AgentOptions: ...
    def new_agent(self, row: Row, snap: Snapshot, provider: str | None = None) -> ActionResult: ...
    def lazygit(self, row: Row, snap: Snapshot) -> ActionResult: ...
    def rename_session(self, row: Row, snap: Snapshot, new_name: str) -> ActionResult: ...
    def plan_remove(self, row: Row, snap: Snapshot) -> Plan | ActionResult: ...
    def spawn_options(self, row: Row | None, snap: Snapshot) -> SpawnOptions: ...
    def plan_spawn(self, req: SpawnRequest, snap: Snapshot) -> Plan: ...
    def execute(self, plan: Plan, typed: str | None = None, *,
                progress: Progress | None = None,
                cancel: threading.Event | None = None) -> ActionResult: ...


class NullActions:
    """Inert actions (demo mode, tests): never touches the system."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def _na(self, what: str) -> ActionResult:
        return ActionResult(False, f"{what}: not available in this mode")

    def jump(self, row, snap):
        return self._na("jump")

    def open_tab(self, row, snap):
        return self._na("open tab")

    def agent_options(self):
        return AgentOptions(choices=(), default_provider="opencode")

    def new_agent(self, row, snap, provider=None):
        return self._na("new agent")

    def lazygit(self, row, snap):
        return self._na("lazygit")

    def rename_session(self, row, snap, new_name):
        return self._na("rename")

    def plan_remove(self, row, snap):
        return self._na("remove")

    def spawn_options(self, row, snap):
        return SpawnOptions(repos=(), default_repo=None, layouts=())

    def plan_spawn(self, req, snap):
        return Plan(kind="spawn", confirm=None, errors=("spawn is not available in this mode",))

    def execute(self, plan, typed=None, *, progress=None, cancel=None):
        return self._na(plan.kind)
