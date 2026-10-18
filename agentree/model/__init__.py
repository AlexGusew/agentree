"""Pure model layer: linking, filtering, tree rows, detail pane, (de)serialisation.

Re-exports are resolved lazily (PEP 562) so importing one submodule (e.g. agentree.model.tree on
the startup path) does not pull in link/sample/hashlib.
"""
from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "detail_for": "detail",
    "apply_filters": "filters",
    "WorktreeIndex": "link",
    "containing_worktree": "link",
    # `link` the function is not re-exported: the attribute name collides with the submodule
    # agentree.model.link once that is imported. Use `from agentree.model.link import link`.
    "sample_raw": "sample",
    "sample_snapshot": "sample",
    "from_dict": "snapshot",
    "to_dict": "snapshot",
    "build_view": "tree",
    "cycle_worktree_mode": "tree",
    "first_child_key": "tree",
    "parent_key": "tree",
    "render_plain": "tree",
    "resolve_selection": "tree",
    "row_for": "tree",
    "select_move": "tree",
    "toggle": "tree",
    "toggle_fold": "tree",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    mod = _EXPORTS.get(name)
    if mod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{mod}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
