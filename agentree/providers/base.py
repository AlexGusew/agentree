"""Agent provider contract and provider-neutral records.

A provider is one agent CLI (opencode, Claude Code, ...). Core code (gather, live status,
actions, doctor, cli, sample) talks to providers only through `Provider`; the registry
(agentree.providers.registry) maps descriptors (`ProviderSpec`) to lazily imported instances.

Contract rules:
- Importing a provider module / listing descriptors does no executable lookup, process scan,
  file write or thread start.
- Provider instances may own caches, never UI state.
- `build_launch()` is read-only (generating a UUID is allowed; writing settings is not).
- `execute_setup()` routes every mutation through the Runner and honours dry-run; setup steps
  are idempotent infrastructure preparation.
- `fetch_quick()` looks only at the agents it is given (no `ps` discovery).
- Fetch warnings use `source` = provider id.
- A watcher receives only its own provider's agents; `healthy()` reports event-feed health.
- Agent.meta never holds secrets, environment, transcript bodies or live objects.
- Unknown providers fail closed for actions (never reinterpreted as opencode); they still
  deserialize and render.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal, Mapping, Protocol, Sequence

from agentree.types import Agent, ExtraValue, SourceWarning, ZPane

if TYPE_CHECKING:
    from agentree.actions.runner import Runner
    from agentree.config import Config
    from agentree.diagnostics import Check, Probe
    from agentree.sources.proc import Proc

__all__ = [
    "ExtraValue", "PaneTitles", "ProcessRow", "FetchResult",
    "ProviderSpec", "FetchContext", "LaunchRequest", "SetupStep", "Launch", "Watcher",
    "RefreshPolicy", "DoctorJob", "DemoContext", "DemoAgent", "Provider",
]

type PaneTitles = Mapping[tuple[str, int], str]
type ProcessRow = tuple[int, int, str] | Proc
type FetchResult = tuple[tuple[Agent, ...], list[SourceWarning]]


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Static descriptor: cheap, importable without importing the provider module."""
    id: str                                   # stable id: Agent.provider, plan data, --agent
    label: str                                # user-facing name ("Claude Code")
    module: str                               # importable module exporting create_provider(spec)
    command_key: str                          # config key of the launch command ("claude_cmd")
    default_command: str                      # default of that key
    extra_command_dirs: tuple[str, ...] = ()  # searched after PATH (+ exec's usual dirs)
    internal_commands: tuple[str, ...] = ()   # hidden CLI subcommands it owns ("claude-hook")


@dataclass(frozen=True, slots=True)
class FetchContext:
    timeout_s: float
    now: float | None
    pane_titles: Callable[[], PaneTitles]     # deferred zellij pane titles (session, pane id) -> title


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    mode: Literal["pane", "spawn"]
    prompt: str = ""
    layout: str | None = None
    session_id: str | None = None             # pre-generated (plan time) session id, if any
    settings_path: str | None = None
    state_dir: str = ""


@dataclass(frozen=True, slots=True)
class SetupStep:
    operation: str                            # provider-defined, e.g. "write_hook_settings"
    label: str                                # display line
    payload: tuple[tuple[str, ExtraValue], ...] = ()


@dataclass(frozen=True, slots=True)
class Launch:
    argv: tuple[str, ...] | None              # None => no generated command; spawn keeps the named layout
    session_id: str | None = None
    setup: tuple[SetupStep, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class Watcher(Protocol):
    def update(self, agents: Sequence[Agent]) -> None: ...
    def healthy(self) -> Mapping[int, bool]: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RefreshPolicy:
    full_s: float
    quick_s: float | None = None


@dataclass(frozen=True, slots=True)
class DoctorJob:
    section: str
    run: Callable[[], Sequence[Check]]


@dataclass(frozen=True, slots=True)
class DemoContext:
    now: float
    paths: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DemoAgent:
    agent: Agent
    session_name: str | None
    tab_id: int | None
    pane: ZPane | None


class Provider(Protocol):
    id: str
    quick_failure: Literal["keep_snapshot", "keep_provider"]

    def matches_argv(self, argv: Sequence[str]) -> bool: ...
    def find_pids(self, rows: Sequence[ProcessRow]) -> tuple[int, ...]: ...
    def session_id_from_argv(self, argv: Sequence[str]) -> str | None: ...
    def resolve_command(self, cfg: Config) -> tuple[str, ...] | None: ...
    def build_launch(self, cfg: Config, request: LaunchRequest) -> Launch: ...
    def execute_setup(self, step: SetupStep, *, runner: Runner, state_dir: str) -> str: ...
    def fetch_full(self, ctx: FetchContext) -> FetchResult: ...
    def fetch_quick(self, agents: Sequence[Agent], ctx: FetchContext) -> FetchResult: ...
    def fetch_budget_s(self, timeout_s: float, *, quick: bool) -> float: ...
    def start_watcher(self, poke: Callable[[], None]) -> Watcher | None: ...
    def refresh_policy(self, agents: Sequence[Agent], watcher: Watcher | None) -> RefreshPolicy: ...
    def find_launched_pids(self, session: str, pane_ids: frozenset[int], *, session_id: str | None,
                           timeout_s: float) -> tuple[int, ...]: ...
    def doctor_jobs(self, cfg: Config, probe: Probe) -> tuple[DoctorJob, ...]: ...
    def run_internal(self, command: str, argv: Sequence[str]) -> int: ...
    def demo_agents(self, ctx: DemoContext) -> tuple[DemoAgent, ...]: ...
