"""Provider registry: static descriptors, lazy import, lookup.

Importing this module (and listing descriptors) never imports a concrete provider, looks up an
executable, scans processes, writes files or starts threads. `get()` imports the provider
module on first use (importlib) and caches the instance; every provider module exports
`create_provider(spec) -> Provider`.

Order is deterministic and meaningful: opencode before claude (PID precedence when agents are
merged). Tests inject providers with `override(...)` instead of mutating the globals.
"""
from __future__ import annotations

import contextlib
import importlib
import threading
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Sequence

from agentree.providers.base import Provider, ProviderSpec

if TYPE_CHECKING:
    from agentree.config import Config

__all__ = ["SPECS", "specs", "ids", "spec", "get", "all_providers", "internal_owner", "default_id",
           "missing_command_message", "override", "validate"]

CONFIG_FILE_HINT = "~/.config/agentree/config.toml"

SPECS: tuple[ProviderSpec, ...] = (
    ProviderSpec(id="opencode", label="opencode", module="agentree.providers.opencode",
                 command_key="opencode_cmd", default_command="opencode"),
    ProviderSpec(id="claude", label="Claude Code", module="agentree.providers.claude",
                 command_key="claude_cmd", default_command="claude",
                 extra_command_dirs=("~/.local/bin", "~/.claude/local"),
                 internal_commands=("claude-hook",)),
)

_lock = threading.RLock()
_instances: dict[str, Provider] = {}


def validate(items: Sequence[ProviderSpec]) -> tuple[ProviderSpec, ...]:
    """The descriptors as a tuple; ValueError unless ids, command keys and internal commands
    are unique (and every entry is a ProviderSpec with a non-empty id)."""
    out = tuple(items)
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    seen_cmds: set[str] = set()
    for s in out:
        if not isinstance(s, ProviderSpec):
            raise ValueError(f"not a ProviderSpec: {s!r}")
        if not s.id:
            raise ValueError("provider id must not be empty")
        if s.id in seen_ids:
            raise ValueError(f"duplicate provider id {s.id!r}")
        if s.command_key in seen_keys:
            raise ValueError(f"duplicate provider command key {s.command_key!r}")
        seen_ids.add(s.id)
        seen_keys.add(s.command_key)
        for c in s.internal_commands:
            if c in seen_cmds:
                raise ValueError(f"duplicate provider internal command {c!r}")
            seen_cmds.add(c)
    return out


validate(SPECS)


def specs() -> tuple[ProviderSpec, ...]:
    return SPECS


def ids() -> tuple[str, ...]:
    return tuple(s.id for s in SPECS)


def spec(provider_id: str) -> ProviderSpec:
    """The descriptor for `provider_id`; KeyError if unknown."""
    for s in SPECS:
        if s.id == provider_id:
            return s
    raise KeyError(provider_id)


def get(provider_id: str) -> Provider:
    """The (cached) provider instance; KeyError if unknown. Imports the provider module lazily."""
    with _lock:
        inst = _instances.get(provider_id)
        if inst is not None:
            return inst
        s = spec(provider_id)
        mod = importlib.import_module(s.module)
        inst = mod.create_provider(s)
        _instances[provider_id] = inst
        return inst


def all_providers() -> tuple[Provider, ...]:
    """Every registered provider, in registry order (imports them)."""
    return tuple(get(s.id) for s in SPECS)


def internal_owner(command: str) -> ProviderSpec | None:
    """The descriptor owning hidden CLI subcommand `command` (e.g. "claude-hook"), else None."""
    for s in SPECS:
        if command in s.internal_commands:
            return s
    return None


def default_id(cfg: Config | Any) -> str:
    """cfg.default_agent if it is a registered id, else "opencode" (if registered), else the
    first registered id."""
    known = ids()
    d = getattr(cfg, "default_agent", None)
    if isinstance(d, str) and d in known:
        return d
    if "opencode" in known:
        return "opencode"
    return known[0] if known else "opencode"


def missing_command_message(provider_id: str, cfg: Config | Any) -> str:
    """The refusal when a provider's launch command cannot be resolved (wording of the old
    actions.agent.missing_message)."""
    try:
        s = spec(provider_id)
    except KeyError:
        return f"unknown agent provider {provider_id!r}"
    from agentree.providers.command import configured_command

    cmd = configured_command(s, cfg)
    if cmd is None:
        cmd = getattr(cfg, s.command_key, provider_id)
    return f"{provider_id} command not found: {cmd!r} (set {s.command_key} in {CONFIG_FILE_HINT})"


@contextlib.contextmanager
def override(specs: Sequence[ProviderSpec] | None = None,
             providers: Mapping[str, Provider] | None = None) -> Iterator[None]:
    """Test helper: temporarily replace the descriptors (`specs`, default: keep the current
    ones) and the instance cache (`providers`: id -> instance, used instead of importing; ids
    not given are imported lazily as usual). Everything is restored on exit."""
    global SPECS, _instances
    new_specs = validate(SPECS if specs is None else specs)
    with _lock:
        saved = (SPECS, _instances)
        SPECS = new_specs
        _instances = dict(providers or {})
    try:
        yield
    finally:
        with _lock:
            SPECS, _instances = saved
