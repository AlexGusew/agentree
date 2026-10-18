"""Where agentree keeps its files, following the XDG base directory spec.

  config  $AGENTREE_CONFIG, else $XDG_CONFIG_HOME/agentree/config.toml (default ~/.config)
  state   $AGENTREE_STATE_DIR, else $XDG_STATE_HOME/agentree (default ~/.local/state)

Stdlib `os` only: imported on the startup path.
"""
from __future__ import annotations

import os
from typing import Mapping


def config_path(environ: Mapping[str, str] | None = None, home: str | None = None) -> str:
    e = os.environ if environ is None else environ
    if e.get("AGENTREE_CONFIG"):
        return _expand(e["AGENTREE_CONFIG"], home)
    return os.path.join(_xdg(e, "XDG_CONFIG_HOME", ".config", home), "agentree", "config.toml")


def state_dir(environ: Mapping[str, str] | None = None, home: str | None = None) -> str:
    e = os.environ if environ is None else environ
    if e.get("AGENTREE_STATE_DIR"):
        return _expand(e["AGENTREE_STATE_DIR"], home)
    return os.path.join(_xdg(e, "XDG_STATE_HOME", os.path.join(".local", "state"), home), "agentree")


def _xdg(e: Mapping[str, str], var: str, default: str, home: str | None) -> str:
    v = e.get(var)
    if v and os.path.isabs(v):              # the spec says relative values are invalid: ignore them
        return v
    return os.path.join(home or os.path.expanduser("~"), default)


def _expand(p: str, home: str | None) -> str:
    if home is not None and (p == "~" or p.startswith("~/")):
        return home + p[1:]
    return os.path.expanduser(p)
