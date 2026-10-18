"""Persistent state under ~/.local/state/agentree ($XDG_STATE_HOME; override with $AGENTREE_STATE_DIR).

ui.json        filter + fold state
links.json     spawn-flow placement records (guarded by links.lock via fcntl)
snapshot.json  last snapshot, rendered immediately on startup (stale=True)
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import functools
import json
import os
import tempfile
import typing
from typing import Any, Iterator

from agentree import paths
from agentree.model.fmt import norm
from agentree.model.snapshot import SnapshotFormatError, decode_value, from_dict, to_dict
from agentree.types import Snapshot, UiState


def state_dir() -> str:
    return paths.state_dir()


def _path(name: str) -> str:
    return os.path.join(state_dir(), name)


def ui_path() -> str:
    return _path("ui.json")


def links_path() -> str:
    return _path("links.json")


def cache_path() -> str:
    return _path("snapshot.json")


def atomic_write(path: str, data: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(d)


def _fsync_dir(d: str) -> None:
    """Persist the rename itself (best effort; some filesystems refuse fsync on directories)."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None


# --------------------------------------------------------------------------- ui.json

def load_ui(defaults: UiState | None = None) -> UiState:
    """Load persisted UI state over `defaults`.

    Each field is type-checked against UiState's declaration; a bad field falls back to its
    default individually (folds keep their well-formed entries). Never raises.
    """
    base = defaults or UiState()
    try:
        d = _read_json(ui_path())
        if not isinstance(d, dict):
            return base
        hints = _ui_hints()
        kw: dict[str, Any] = {}
        for f in dataclasses.fields(UiState):
            if f.name not in d:
                continue
            raw = d[f.name]
            if f.name == "folds":
                if isinstance(raw, list):
                    folds = []
                    for item in raw:
                        try:
                            folds.append(decode_value(tuple[str, bool], item))
                        except SnapshotFormatError:
                            continue
                    kw["folds"] = tuple(folds)
                continue
            try:
                kw[f.name] = decode_value(hints[f.name], raw)
            except SnapshotFormatError:
                continue
        return dataclasses.replace(base, **kw)
    except Exception:
        return base


@functools.cache
def _ui_hints() -> dict[str, Any]:
    return typing.get_type_hints(UiState)


def save_ui(ui: UiState) -> None:
    d = {
        "worktree_mode": ui.worktree_mode,
        "show_agents": ui.show_agents,
        "show_panes": ui.show_panes,
        "show_detached": ui.show_detached,
        "text": ui.text,
        "folds": [[k, v] for k, v in ui.folds],
        "selected": ui.selected,
    }
    atomic_write(ui_path(), json.dumps(d, indent=1) + "\n")


# --------------------------------------------------------------------------- links.json

@contextlib.contextmanager
def links_lock() -> Iterator[None]:
    os.makedirs(state_dir(), exist_ok=True)
    with open(_path("links.lock"), "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load_links_unlocked() -> list[dict]:
    d = _read_json(links_path())
    if not isinstance(d, list):
        return []
    return [r for r in d if isinstance(r, dict) and isinstance(r.get("worktreePath"), str)]


def load_links() -> list[dict]:
    return _load_links_unlocked()


def _write_links(recs: list[dict]) -> None:
    atomic_write(links_path(), json.dumps(recs, indent=1) + "\n")


def upsert_link(rec: dict) -> None:
    """Insert or replace the record for rec['worktreePath'] (keys: worktreePath, session, tabName, agentPids)."""
    path = rec.get("worktreePath")
    if not isinstance(path, str) or not path:
        raise ValueError("link record needs worktreePath")
    new = dict(rec)
    new["worktreePath"] = norm(path)
    new.setdefault("agentPids", [])
    with links_lock():
        recs = [r for r in _load_links_unlocked() if norm(r["worktreePath"]) != new["worktreePath"]]
        recs.append(new)
        _write_links(recs)


def remove_link(worktree_path: str) -> bool:
    """Remove the record for a worktree; returns True if one was removed."""
    p = norm(worktree_path)
    with links_lock():
        recs = _load_links_unlocked()
        keep = [r for r in recs if norm(r["worktreePath"]) != p]
        if len(keep) == len(recs):
            return False
        _write_links(keep)
        return True


def rename_link_session(old: str, new: str) -> int:
    """Point every record of session `old` at `new`; returns the number of records changed."""
    with links_lock():
        recs = _load_links_unlocked()
        n = 0
        for r in recs:
            if r.get("session") == old:
                r["session"] = new
                n += 1
        if n:
            _write_links(recs)
        return n


def swap_link(rec: dict) -> dict | None:
    """Write `rec` for its worktree under the lock; returns the record it replaced (None if new).
    The pair (previous, rec) is what a rollback needs for `restore_link`."""
    path = rec.get("worktreePath")
    if not isinstance(path, str) or not path:
        raise ValueError("link record needs worktreePath")
    new = dict(rec)
    new["worktreePath"] = norm(path)
    new.setdefault("agentPids", [])
    with links_lock():
        recs = _load_links_unlocked()
        prev = next((r for r in recs if norm(r["worktreePath"]) == new["worktreePath"]), None)
        recs = [r for r in recs if norm(r["worktreePath"]) != new["worktreePath"]]
        recs.append(new)
        _write_links(recs)
        return prev


def swap_link_journaled(rec: dict, persist_prev) -> dict | None:
    """`swap_link` with the replaced record made durable first: under links.lock, read the current
    record for the worktree, call `persist_prev(previous)` (the caller writes it to its journal;
    if that raises, nothing is swapped), then write `rec`. A crash after the swap leaves the
    previous record recoverable from the journal."""
    path = rec.get("worktreePath")
    if not isinstance(path, str) or not path:
        raise ValueError("link record needs worktreePath")
    new = dict(rec)
    new["worktreePath"] = norm(path)
    new.setdefault("agentPids", [])
    with links_lock():
        recs = _load_links_unlocked()
        prev = next((r for r in recs if norm(r["worktreePath"]) == new["worktreePath"]), None)
        persist_prev(prev)
        recs = [r for r in recs if norm(r["worktreePath"]) != new["worktreePath"]]
        recs.append(new)
        _write_links(recs)
        return prev


def merge_link(worktree_path: str, mutate) -> dict | None:
    """Read-modify-write one worktree's record under a single links.lock.

    `mutate(current | None)` gets a copy of the current record (None if there is none) and
    returns the record to store (None: drop it / store nothing). Fields it does not touch are
    kept as they are; `worktreePath` is always the normalised `worktree_path`. Returns what was
    stored. Concurrent mergers are serialised, so none of them loses another's update."""
    if not isinstance(worktree_path, str) or not worktree_path:
        raise ValueError("merge_link needs a worktree path")
    p = norm(worktree_path)
    with links_lock():
        recs = _load_links_unlocked()
        cur = next((r for r in recs if norm(r["worktreePath"]) == p), None)
        new = mutate(dict(cur) if cur is not None else None)
        rest = [r for r in recs if norm(r["worktreePath"]) != p]
        if new is None:
            if cur is not None:
                _write_links(rest)
            return None
        new = dict(new)
        new["worktreePath"] = p
        new.setdefault("agentPids", [])
        if new != cur:
            if cur is None:
                _write_links(recs + [new])
            else:                                # in place: unrelated records keep their order
                out, done = [], False
                for r in recs:
                    if norm(r["worktreePath"]) != p:
                        out.append(r)
                    elif not done:
                        out.append(new)
                        done = True
                _write_links(out)
        return new


def restore_link(written: dict, previous: dict | None) -> bool:
    """Undo `swap_link`: only if the current record is still exactly `written`, put `previous`
    back (or drop the record). Returns False (nothing changed) if someone wrote it since."""
    p = norm(written["worktreePath"])
    want = dict(written, worktreePath=p)
    want.setdefault("agentPids", [])
    with links_lock():
        recs = _load_links_unlocked()
        cur = next((r for r in recs if norm(r["worktreePath"]) == p), None)
        if cur != want:
            return False
        recs = [r for r in recs if norm(r["worktreePath"]) != p]
        if previous is not None:
            recs.append(previous)
        _write_links(recs)
        return True


# --------------------------------------------------------------------------- journals
# A spawn writes <state>/journal/<run-id>.json while it runs (what it is about to create / has
# created, with identities) and deletes it when it ends; a leftover file means a crash mid-way.

def journal_dir() -> str:
    return _path("journal")


def write_journal(run_id: str, data: dict) -> None:
    atomic_write(os.path.join(journal_dir(), f"{run_id}.json"), json.dumps(data, indent=1) + "\n")


def delete_journal(run_id: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(os.path.join(journal_dir(), f"{run_id}.json"))


def list_journals() -> list[dict]:
    """Leftover journals (crashed runs): [{"run_id", "path", "data"}] sorted by name."""
    out: list[dict] = []
    try:
        names = sorted(os.listdir(journal_dir()))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json") or n.startswith("."):
            continue
        path = os.path.join(journal_dir(), n)
        out.append({"run_id": n[:-5], "path": path, "data": _read_json(path)})
    return out


# --------------------------------------------------------------------------- snapshot cache

def save_cache(snapshot: Snapshot) -> None:
    d = to_dict(dataclasses.replace(snapshot, stale=False))
    atomic_write(cache_path(), json.dumps(d, separators=(",", ":")))


def load_cache() -> Snapshot | None:
    """Last saved snapshot marked stale, or None on any problem (missing, corrupt, schema
    mismatch). Never raises: a bad cache is just a cache miss."""
    try:
        d = _read_json(cache_path())
        if not isinstance(d, dict):
            return None
        return dataclasses.replace(from_dict(d), stale=True)
    except Exception:
        return None
