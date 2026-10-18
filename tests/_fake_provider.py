"""A scriptable agent provider for action / cli / doctor tests (injected with
registry.override; never mutates the global registry).

`FakeProvider` implements the whole Provider protocol with recorded calls. By default it acts
like a third provider ("codex"): launch argv = the resolved `codex_cmd` (+ `--session <id>`
+ the prompt), one setup step writing a small settings file through the runner, a pid finder
returning `self.pids`, two doctor jobs and an internal command.
"""
from __future__ import annotations

import contextlib
import os
import shlex
from typing import Any, Literal, Sequence

from agentree.diagnostics import Check
from agentree.providers import command as cmdmod
from agentree.providers import registry
from agentree.providers.base import (
    DemoAgent,
    DoctorJob,
    Launch,
    LaunchRequest,
    ProviderSpec,
    RefreshPolicy,
    SetupStep,
)

CODEX = ProviderSpec(id="codex", label="Codex", module="tests._fake_provider_absent_module",
                     command_key="codex_cmd", default_command="codex --full-auto",
                     internal_commands=("codex-hook",))


class FakeProvider:
    quick_failure: Literal["keep_snapshot", "keep_provider"] = "keep_provider"

    def __init__(self, spec: ProviderSpec = CODEX, *, session_ids: bool = True, setup: bool = True,
                 spawn_without_prompt: bool = False, pids: Sequence[int] = ()) -> None:
        self.spec = spec
        self.id = spec.id
        self.session_ids = session_ids              # pane/spawn launches carry `--session <id>`
        self.setup = setup                          # launches carry one settings-file setup step
        self.spawn_without_prompt = spawn_without_prompt   # layout "agent" starts it without a prompt
        self.pids = list(pids)
        self.calls: list[tuple] = []
        self._n = 0

    # ---------------------------------------------------------------- identity
    def exe_name(self) -> str:
        return os.path.basename(shlex.split(self.spec.default_command)[0])

    def matches_argv(self, argv: Sequence[str]) -> bool:
        return bool(argv) and os.path.basename(argv[0]) == self.exe_name() and "mcp" not in argv[1:2]

    def find_pids(self, rows) -> tuple[int, ...]:
        return ()

    def session_id_from_argv(self, argv: Sequence[str]) -> str | None:
        a = list(argv)
        return a[a.index("--session") + 1] if "--session" in a[:-1] else None

    # ---------------------------------------------------------------- launch
    def resolve_command(self, cfg: Any) -> tuple[str, ...] | None:
        return cmdmod.resolve_for(self.spec, cfg)

    def settings_path(self, state_dir: str) -> str:
        return os.path.join(state_dir or "/tmp", self.id, "settings.json")

    def build_launch(self, cfg: Any, request: LaunchRequest) -> Launch:
        self.calls.append(("build_launch", request))
        base = self.resolve_command(cfg)
        if base is None:
            raw = cmdmod.configured_command(self.spec, cfg) or self.spec.default_command
            parts = shlex.split(raw)
            base = (os.path.expanduser(parts[0]), *parts[1:])
        if request.mode == "spawn" and not request.prompt and not (
                self.spawn_without_prompt and request.layout == "agent"):
            return Launch(argv=None, warnings=(f"no prompt: {self.id} is not started",))
        sid = None
        if self.session_ids:
            sid = request.session_id
            if sid is None:
                self._n += 1
                sid = f"{self.id}-sid-{self._n}"
        argv = [*base, *(["--session", sid] if sid else [])]
        if request.prompt:
            argv += ["--", request.prompt]
        setup: tuple[SetupStep, ...] = ()
        if self.setup:
            path = request.settings_path or self.settings_path(request.state_dir)
            setup = (SetupStep("write_settings", f"write {self.id} settings {path}", (("path", path),)),)
        return Launch(argv=tuple(argv), session_id=sid, setup=setup)

    def execute_setup(self, step: SetupStep, *, runner: Any, state_dir: str) -> str:
        self.calls.append(("execute_setup", step, state_dir))
        payload = dict(step.payload)
        path = str(payload["path"])

        def write() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}\n")
        runner.internal(step.label, write)
        return "settings written"

    def find_launched_pids(self, session: str, pane_ids: frozenset[int], *, session_id: str | None,
                           timeout_s: float) -> tuple[int, ...]:
        self.calls.append(("find_launched_pids", session, pane_ids, session_id, timeout_s))
        return tuple(self.pids)

    # ---------------------------------------------------------------- status (unused by actions)
    def fetch_full(self, ctx):
        return (), []

    def fetch_quick(self, agents, ctx):
        return tuple(agents), []

    def fetch_budget_s(self, timeout_s: float, *, quick: bool) -> float:
        return timeout_s

    def start_watcher(self, poke):
        return None

    def refresh_policy(self, agents, watcher) -> RefreshPolicy:
        return RefreshPolicy(10.0)

    # ---------------------------------------------------------------- doctor / cli / demo
    def doctor_jobs(self, cfg: Any, probe: Any) -> tuple[DoctorJob, ...]:
        self.calls.append(("doctor_jobs", cfg, probe))
        ok = self.resolve_command(cfg) is not None
        return (
            DoctorJob("versions", lambda: [Check("versions", "ok", f"{self.id} 9.9.9")]),
            DoctorJob(self.id, lambda: [Check(self.id, "ok" if ok else "warn",
                                              f"{self.spec.command_key} {'found' if ok else 'not found'}")]),
        )

    def run_internal(self, command: str, argv: Sequence[str]) -> int:
        self.calls.append(("run_internal", command, tuple(argv)))
        return 7

    def demo_agents(self, ctx) -> tuple[DemoAgent, ...]:
        return ()


@contextlib.contextmanager
def injected(*fakes: FakeProvider, replace: bool = False):
    """The registry with `fakes` injected: added after the real descriptors (a fake whose id is
    already registered replaces that provider's instance, keeping its descriptor), or — with
    replace=True — as the only providers."""
    known = {s.id for s in registry.SPECS}
    if replace:
        specs = tuple(f.spec for f in fakes)
    else:
        specs = (*registry.SPECS, *(f.spec for f in fakes if f.spec.id not in known))
    with registry.override(specs=specs, providers={f.id: f for f in fakes}):
        yield


def inject(test, *fakes: FakeProvider, replace: bool = False) -> None:
    """injected(...) for the rest of a unittest test (entered now, exited at cleanup)."""
    cm = injected(*fakes, replace=replace)
    cm.__enter__()
    test.addCleanup(cm.__exit__, None, None, None)
