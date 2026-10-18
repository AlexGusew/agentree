"""Rename a live session (`r`): `zellij --session OLD action rename-session NEW` (zellij 0.45.1).

The new name is validated (no '/', no whitespace or control characters, socket path short enough
for macOS' 104-byte sun_path) and must not exist in any socket dir, live or exited. The old name
must be live in exactly one socket dir and be the same server as in the snapshot. links.json
records follow the rename. Exited sessions are refused (zellij cannot rename a resurrectable
session). Renaming the session agentree runs in closes agentree (close=True): its ZELLIJ_SESSION_NAME and
single-instance claim refer to the old name.
"""
from __future__ import annotations

import os
import re

from agentree.actions.api import ActionResult
from agentree.actions.jump import Navigator, _Fail, find_session
from agentree.types import Row, SessionNode, Snapshot, ZSession

SUN_PATH_MAX = 104
_BAD = re.compile(r"[/\s\x00-\x1f\x7f]")


def validate_name(name: str, socket_dir: str | None) -> str | None:
    if not name:
        return "the new name is empty"
    if _BAD.search(name):
        return "session names cannot contain '/', spaces or control characters"
    if name in (".", ".."):
        return "invalid session name"
    sock = os.path.join(socket_dir or "/tmp/zellij-0", "contract_version_1", name)
    if len(sock.encode()) >= SUN_PATH_MAX:
        return f"name too long for the zellij socket path ({len(sock.encode())} ≥ {SUN_PATH_MAX} bytes)"
    return None


def rename_session(row: Row, snap: Snapshot, new: str, nav: Navigator, links=None) -> ActionResult:
    try:
        return _rename(row, snap, new.strip(), nav, links)
    except _Fail as e:
        return nav.fail(e)


def _rename(row: Row, snap: Snapshot, new: str, nav: Navigator, links) -> ActionResult:
    if links is None:
        from agentree import state as links  # type: ignore[no-redef]
    node = row.ref if isinstance(row.ref, SessionNode) else find_session(snap, row.session)
    if node is None:
        raise _Fail("rename: select a session")
    old = node.session
    if old.state == "exited":
        raise _Fail(f"{old.name} has exited: zellij cannot rename a resurrectable session")
    if new == old.name:
        raise _Fail("same name — nothing to do")
    fresh = nav.fresh_session(old.name)
    if fresh.live_dir is None:
        raise _Fail(f"{old.name} has exited — refresh (R)")
    nav.check_incarnation(old, fresh, snap.taken_at)
    why = validate_name(new, fresh.live_dir)
    if why:
        raise _Fail(why)
    if nav.lookup_session(new) is not None:
        raise _Fail(f"a session named {new} already exists")
    s = ZSession(old.name, "attached", old.created_age_s, fresh.live_dir, old.clients)
    r = nav.runner.act(*nav.zj(s, "rename-session", new))
    if not r.ok:
        raise _Fail(f"rename-session: {(r.stderr or r.stdout).strip()[:160] or r.code}")
    if nav.runner.dry_run:
        return nav.finish(True, "")
    after = nav.lookup_session(new)
    if after is None or after.live_dir is None:
        raise _Fail(f"zellij did not report {new} after the rename — refresh (R)")
    own = nav.env.is_current(old.name, fresh.live_dir)
    try:
        nav.runner.internal(f"links.json: session {old.name} -> {new}",
                            lambda: links.rename_link_session(old.name, new))
        partial = ""
    except (OSError, ValueError) as e:
        partial = f"; links.json not updated ({e}) — records still name {old.name}"
    if own:
        # agentree's own session: ZELLIJ_SESSION_NAME and the instance claim are stale now -> close agentree
        nav.renamed_own = new
        return ActionResult(True, f"renamed {old.name} → {new} (agentree's own session; closing){partial}",
                            close=True, refresh=True)
    return ActionResult(True, f"renamed {old.name} → {new}{partial}", refresh=True)
