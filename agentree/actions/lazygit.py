"""`g`: lazygit in a floating zellij pane at the selected worktree.

The binary comes from config `lazygit_cmd` (default "lazygit"), resolved on PATH plus the usual
extra dirs; missing -> a message, nothing touched. Otherwise the same primitive as `o`
(agentree.actions.agent.open_pane): ensure the worktree's tab; under the session lock validate the tab
id, exactly one client, `new-pane --tab-id <id> --cwd <wt> --name lazygit --floating
--close-on-exit -- <lazygit>`, verified in a fresh listing (new id, floating, in that tab, cwd
inside the worktree); then go-to-tab-by-id + focus-pane-id (floating panes take it; read back
from list-clients) + kitty window, and close.
"""
from __future__ import annotations

from typing import Any

from agentree.actions.agent import PaneSpec, open_pane
from agentree.actions.api import ActionResult
from agentree.actions.jump import Navigator
from agentree.providers.command import resolve_command
from agentree.types import Row, Snapshot

MISSING = ("lazygit not installed — see https://github.com/jesseduffield/lazygit#installation "
           "(or set lazygit_cmd in ~/.config/agentree/config.toml)")


def lazygit(nav: Navigator, row: Row, snap: Snapshot, *, cfg: Any, state_dir: str) -> ActionResult:
    cmd = resolve_command(getattr(cfg, "lazygit_cmd", None), "lazygit")
    if cmd is None:
        return ActionResult(False, MISSING)
    _o, res = open_pane(nav, row, snap, PaneSpec("lazygit", "lazygit", "lazygit", cmd, floating=True),
                        state_dir=state_dir, tab_layout=getattr(cfg, "tab_layout", None))
    return res
