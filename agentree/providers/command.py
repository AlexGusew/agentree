"""Shared launch-command resolution for agent providers (and other configured commands).

`resolve_command` is copied verbatim from agentree.actions.agent (which keeps its own copy until the
actions lane switches over). No module-level imports of agentree: resolving touches the filesystem
only when called.
"""
from __future__ import annotations

import os
import shlex
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentree.providers.base import ProviderSpec

__all__ = ["resolve_command", "configured_command", "resolve_for"]


def resolve_command(cmd: str | None, default: str, *, extra_dirs: tuple[str, ...] = ()
                    ) -> tuple[str, ...] | None:
    """argv for a configured command string (`~` expanded, argv0 resolved on PATH + the usual
    extra dirs, then `extra_dirs`); None if it cannot be found / is not executable."""
    from agentree.actions import exec as ex

    if not isinstance(cmd, str):
        cmd = default
    try:
        parts = shlex.split(cmd.strip() or default)
    except ValueError:
        return None
    if not parts:
        return None
    exe = os.path.expanduser(parts[0])
    if "/" in exe:
        if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
            return None
        return (exe, *parts[1:])
    found = ex.which(exe)
    if not found:
        for d in extra_dirs:
            cand = os.path.join(os.path.expanduser(d), exe)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                found = cand
                break
    return (found, *parts[1:]) if found else None


def configured_command(spec: ProviderSpec, cfg: Any) -> str | None:
    """The raw configured command string for `spec` (cfg.provider_command(id)); duck-typed
    configs without provider_command fall back to the `<id>_cmd` attribute. None if unset /
    not a string (resolve_command then uses its fallback)."""
    fn = getattr(cfg, "provider_command", None)
    if callable(fn):
        try:
            v = fn(spec.id)
        except KeyError:
            v = None
    else:
        v = getattr(cfg, spec.command_key, None)
    return v if isinstance(v, str) else None


def resolve_for(spec: ProviderSpec, cfg: Any, *, fallback: str | None = None) -> tuple[str, ...] | None:
    """Resolved base argv of a provider's launch command: the configured command
    (cfg.provider_command(spec.id)) on PATH + exec's usual dirs, then spec.extra_command_dirs.
    A blank / missing command falls back to `fallback`, default the bare executable named like
    the provider id (exactly the old resolvers: opencode -> "opencode", claude -> "claude")."""
    return resolve_command(configured_command(spec, cfg), fallback or spec.id,
                           extra_dirs=spec.extra_command_dirs)
