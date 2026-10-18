"""agentree configuration: ~/.config/agentree/config.toml ($XDG_CONFIG_HOME; override with $AGENTREE_CONFIG).

All keys optional. Unknown keys or wrong types produce warnings, never a crash.
"""
from __future__ import annotations

import json
import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from typing import Any

from agentree.providers import registry
from agentree.types import WORKTREE_MODES, UiState

DEFAULT_INSTALL_COMMANDS: dict[str, str] = {
    "pnpm-lock.yaml": "pnpm install",
    "package-lock.json": "npm install",
    "bun.lock": "bun install",
    "bun.lockb": "bun install",
    "yarn.lock": "yarn install",
    "Cargo.lock": "cargo fetch",
}

DEFAULT_FILTERS: dict[str, Any] = {
    "worktree_mode": "all",
    "agents": True,
    "panes": False,
    "detached": True,
}


@dataclass(frozen=True)
class Config:
    repo_roots: tuple[str, ...] = ("~/Projects/*",)
    worktree_template: str = "{repo}/.worktrees/{slug}"
    branch_prefix: str = "agent/"
    accent: str = "#e5b454"
    layouts: tuple[str, ...] = ("agent", "default")
    default_filters: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_FILTERS))
    kitty_socket_glob: str = "/tmp/kitty.sock-*"
    zellij_socket_dirs: tuple[str, ...] = ()          # empty = auto-detect
    install_commands: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INSTALL_COMMANDS))
    source_timeout_s: float = 1.0
    git_budget_s: float = 2.5                          # wall budget for the git inventory (status pass)
    # Agent launch commands, provider id -> command string (TOML key: the provider's
    # command_key, e.g. opencode_cmd). Missing ids fall back to the descriptor default.
    provider_commands: dict[str, str] = field(default_factory=dict)
    # Legacy aliases of provider_commands["opencode"] / ["claude"], equal to them after
    # construction. None = not given; when given (Config(claude_cmd=...), dataclasses.replace)
    # they win over provider_commands.
    opencode_cmd: str | None = None
    claude_cmd: str | None = None
    default_agent: str = "opencode"                    # a registry provider id: spawn dialog default
    lazygit_cmd: str = "lazygit"                       # `g`: command run in a floating pane (PATH lookup)
    tab_layout: str | None = None                      # zellij layout for tabs opened with `t` (None: default)
    path: str | None = None                            # file it was loaded from (None = defaults)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        cmds = dict(self.provider_commands or {})
        for name, pid in _LEGACY_CMD_FIELDS.items():
            v = getattr(self, name)
            if v is None:
                v = cmds.get(pid)
            if v is None:
                v = _spec_default(pid, name)
            cmds[pid] = v
            object.__setattr__(self, name, v)
        object.__setattr__(self, "provider_commands", cmds)

    def provider_command(self, provider_id: str) -> str:
        """The configured launch command of a provider, else its descriptor default; KeyError
        for an id that is neither configured nor registered."""
        v = self.provider_commands.get(provider_id)
        if v is not None:
            return v
        return registry.spec(provider_id).default_command

    def default_ui(self) -> UiState:
        f = self.default_filters
        return UiState(
            worktree_mode=f.get("worktree_mode", "all"),
            show_agents=bool(f.get("agents", True)),
            show_panes=bool(f.get("panes", False)),
            show_detached=bool(f.get("detached", True)),
        )

    def expanded_repo_roots(self) -> tuple[str, ...]:
        return tuple(os.path.expanduser(p) for p in self.repo_roots)

    def worktree_path(self, repo_root: str, slug: str) -> str:
        repo_root = repo_root.rstrip("/")
        return os.path.expanduser(self.worktree_template.format(
            repo=repo_root, slug=slug, name=os.path.basename(repo_root)))


# legacy Config fields that alias provider_commands[<id>] (field name -> provider id)
_LEGACY_CMD_FIELDS: dict[str, str] = {"opencode_cmd": "opencode", "claude_cmd": "claude"}
_LEGACY_CMD_DEFAULTS: dict[str, str] = {"opencode_cmd": "opencode", "claude_cmd": "claude"}


def _spec_default(provider_id: str, field_name: str) -> str:
    try:
        return registry.spec(provider_id).default_command
    except KeyError:                       # provider not registered (tests overriding the registry)
        return _LEGACY_CMD_DEFAULTS[field_name]


_STATIC_KEYS = ({f.name for f in fields(Config)}
                - {"path", "warnings", "provider_commands"} - set(_LEGACY_CMD_FIELDS))
_STR_KEYS = ("worktree_template", "branch_prefix", "accent", "kitty_socket_glob", "tab_layout",
             "lazygit_cmd", "default_agent")


def _command_keys() -> dict[str, str]:
    """TOML command key -> provider id, from the registry descriptors."""
    return {s.command_key: s.id for s in registry.specs()}


def config_path() -> str:
    from agentree.paths import config_path

    return config_path()


def _str_list(v: Any) -> tuple[str, ...] | None:
    if isinstance(v, str):
        return (v,)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return tuple(v)
    return None


def from_mapping(data: dict[str, Any], path: str | None = None) -> Config:
    warns: list[str] = []
    kw: dict[str, Any] = {}
    cmd_keys = _command_keys()
    cmds: dict[str, str] = {}
    for key, val in data.items():
        if key in cmd_keys:
            if not isinstance(val, str):
                warns.append(f"{key}: expected a string")
                continue
            cmds[cmd_keys[key]] = val
            continue
        if key not in _STATIC_KEYS:
            warns.append(f"unknown config key {key!r}")
            continue
        if key in ("repo_roots", "layouts", "zellij_socket_dirs"):
            lst = _str_list(val)
            if lst is None:
                warns.append(f"{key}: expected a list of strings")
                continue
            kw[key] = lst
        elif key in _STR_KEYS:
            if not isinstance(val, str):
                warns.append(f"{key}: expected a string")
                continue
            if key == "default_agent" and val not in registry.ids():
                warns.append(f"default_agent: expected one of {', '.join(registry.ids())}")
                continue
            if key == "worktree_template" and "{slug}" not in val:
                warns.append("worktree_template: must contain {slug}")
                continue
            kw[key] = val
        elif key in ("source_timeout_s", "git_budget_s"):
            if (isinstance(val, bool) or not isinstance(val, (int, float))
                    or not math.isfinite(val) or val <= 0):
                warns.append(f"{key}: expected a positive finite number")
                continue
            kw[key] = float(val)
        elif key == "default_filters":
            if not isinstance(val, dict):
                warns.append(f"{key}: expected a table")
                continue
            df = dict(DEFAULT_FILTERS)
            for fk, fv in val.items():
                if fk not in DEFAULT_FILTERS:
                    warns.append(f"unknown config key 'default_filters.{fk}'")
                elif fk == "worktree_mode":
                    if fv in WORKTREE_MODES:
                        df[fk] = fv
                    else:
                        warns.append(f"default_filters.worktree_mode: expected one of {', '.join(WORKTREE_MODES)}")
                elif isinstance(fv, bool):
                    df[fk] = fv
                else:
                    warns.append(f"default_filters.{fk}: expected true/false")
            kw[key] = df
        elif key == "install_commands":
            if not isinstance(val, dict):
                warns.append(f"{key}: expected a table")
                continue
            ic = dict(DEFAULT_INSTALL_COMMANDS)
            for lk, cmd in val.items():
                if isinstance(cmd, str):
                    ic[lk] = cmd
                elif cmd is False:
                    ic.pop(lk, None)
                else:
                    warns.append(f"install_commands.{lk}: expected a string (or false to disable)")
            kw[key] = ic
    return Config(**kw, provider_commands=cmds, path=path, warnings=tuple(warns))


def load_config(path: str | None = None) -> Config:
    """Load config; missing file -> defaults; parse errors -> defaults + warning."""
    p = path or config_path()
    try:
        with open(p, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return Config(path=p, warnings=(f"config {p}: {e}",))
    return from_mapping(data, path=p)


def example_toml() -> str:
    return _EXAMPLE_HEAD + _agent_example() + _EXAMPLE_TAIL


_EXAMPLE_HEAD = '''\
# agentree configuration — ~/.config/agentree/config.toml (or $AGENTREE_CONFIG). Every key is optional.

# Repositories to scan for worktrees (globs, ~ expanded).
repo_roots = ["~/Projects/*"]

# Where new worktrees go. {repo} = repo root path, {name} = repo basename, {slug} = branch slug.
worktree_template = "{repo}/.worktrees/{slug}"

# Prefix for branches created by the spawn flow.
branch_prefix = "agent/"

# Accent colour (truecolor hex).
accent = "#e5b454"

# zellij layout names offered by the spawn dialog.
layouts = ["agent", "default"]

# kitty remote-control sockets (listen_on unix:/tmp/kitty.sock -> /tmp/kitty.sock-<pid>).
kitty_socket_glob = "/tmp/kitty.sock-*"

# ZELLIJ_SOCKET_DIR candidates; empty = auto-detect.
zellij_socket_dirs = []

# Per-source timeout in seconds.
source_timeout_s = 1.0

# Wall-clock budget for the git inventory (worktree list + `git status` dirty counts).
git_budget_s = 2.5

'''

_EXAMPLE_TAIL = '''\

# zellij layout for tabs opened with `t` (omit for zellij's default tab layout).
# tab_layout = "agent"

[default_filters]
worktree_mode = "all"   # all | with_tab | dirty | hide
agents = true
panes = false
detached = true

# Install command per lockfile (set a key to false to disable it).
[install_commands]
"pnpm-lock.yaml" = "pnpm install"
"package-lock.json" = "npm install"
"bun.lock" = "bun install"
"bun.lockb" = "bun install"
"yarn.lock" = "yarn install"
"Cargo.lock" = "cargo fetch"
'''


def _agent_example() -> str:
    """The agent section of example_toml(): one command key per registered provider, then
    default_agent."""
    out: list[str] = []
    for s in registry.specs():
        art = "an" if s.label[:1].lower() in "aeiou" else "a"
        out.append(f"# Command used to start {art} {s.label} agent.\n{s.command_key} = {json.dumps(s.default_command)}\n\n")
    out.append(f"# Agent preselected in the spawn dialog / `agentree spawn` without --agent: "
               f"{' | '.join(registry.ids())}.\n")
    out.append(f"default_agent = {json.dumps(registry.default_id(None))}\n")
    return "".join(out)
