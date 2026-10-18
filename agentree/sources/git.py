"""git source: repos under configured globs, their worktrees and per-worktree status.

Read-only: only `worktree list`, `status`, `rev-list`, `log`, `symbolic-ref`, with
`--no-optional-locks` so status never rewrites the index.
"""
from __future__ import annotations

import glob
import os
import time
from dataclasses import replace
from typing import NamedTuple

from agentree.actions import exec as ex
from agentree.sources import _par
from agentree.types import Repo, SourceWarning, Worktree

SOURCE = "git"
MAX_WORKERS = 16
STATUS_WORKERS = 8   # measured: more parallel `git status` only adds VFS contention


# --------------------------------------------------------------------------- discovery

def discover_repos(globs: list[str]) -> list[str]:
    """Main checkouts (`.git` is a directory) matched by the globs, realpath'd, sorted, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for g in globs:
        for p in sorted(glob.glob(os.path.expanduser(g))):
            if not os.path.isdir(p) or not os.path.isdir(os.path.join(p, ".git")):
                continue
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    return out


# --------------------------------------------------------------------------- parsers

def parse_worktree_porcelain_z(data: bytes | str) -> list[dict]:
    """`git worktree list --porcelain -z`: NUL-terminated `key[ value]` fields, records separated
    by an empty field. Returns dicts with keys: worktree, HEAD, branch (full ref), detached,
    bare, locked (reason str, possibly ''), prunable (reason str, possibly '')."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", "surrogateescape")
    records: list[dict] = []
    cur: dict = {}
    for fld in data.split("\0"):
        if fld == "":
            if cur:
                records.append(cur)
                cur = {}
            continue
        key, _, val = fld.partition(" ")
        if key in ("detached", "bare"):
            cur[key] = True
        elif key in ("locked", "prunable"):
            cur[key] = val
        else:
            cur[key] = val
    if cur:
        records.append(cur)
    return records


class StatusV2(NamedTuple):
    dirty: int
    ahead: int | None
    behind: int | None
    upstream: str | None


def parse_status_v2(text: str) -> StatusV2:
    """`git status --porcelain=v2 --branch` (newline or NUL separated)."""
    dirty = 0
    ahead = behind = None
    upstream = None
    sep = "\0" if "\0" in text else "\n"
    lines = text.split(sep)
    skip_next = False
    for line in lines:
        if skip_next:  # -z rename records carry the original path as an extra field
            skip_next = False
            continue
        if line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            for tok in line[len("# branch.ab "):].split():
                try:
                    if tok.startswith("+"):
                        ahead = int(tok[1:])
                    elif tok.startswith("-"):
                        behind = int(tok[1:])
                except ValueError:
                    pass
        elif line[:2] in ("1 ", "u ", "? "):
            dirty += 1
        elif line[:2] == "2 ":
            dirty += 1
            if sep == "\0":
                skip_next = True
    return StatusV2(dirty, ahead, behind, upstream)


def parse_rev_list_count(text: str) -> tuple[int, int] | None:
    """`git rev-list --left-right --count base...HEAD` -> (ahead, behind) of HEAD vs base."""
    parts = text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


FOR_EACH_REF_FIELDS = ("refname", "objectname", "upstream", "track", "ab_default", "ct", "subject")


def for_each_ref_format(default_ref: str | None) -> str:
    ab = f"%(ahead-behind:{default_ref})" if default_ref else ""
    return "%00".join(["%(refname)", "%(objectname)", "%(upstream:short)", "%(upstream:track,nobracket)",
                       ab, "%(committerdate:unix)", "%(subject)"])


def parse_for_each_ref(text: str) -> dict[str, dict]:
    """Output of `git for-each-ref --format=<for_each_ref_format()>` -> {full refname: fields}."""
    out: dict[str, dict] = {}
    for line in text.split("\n"):
        if not line:
            continue
        parts = line.split("\0")
        if len(parts) < len(FOR_EACH_REF_FIELDS):
            parts += [""] * (len(FOR_EACH_REF_FIELDS) - len(parts))
        rec = dict(zip(FOR_EACH_REF_FIELDS, parts[: len(FOR_EACH_REF_FIELDS) - 1]))
        rec["subject"] = "\0".join(parts[len(FOR_EACH_REF_FIELDS) - 1:])
        out[rec["refname"]] = rec
    return out


def parse_track(track: str) -> tuple[int, int] | None:
    """`%(upstream:track,nobracket)`: '' (in sync) | 'ahead 1' | 'behind 2' | 'ahead 1, behind 2' | 'gone'."""
    t = track.strip()
    if t == "gone":
        return None
    ahead = behind = 0
    for part in t.split(","):
        k, _, v = part.strip().partition(" ")
        if k == "ahead" and v.isdigit():
            ahead = int(v)
        elif k == "behind" and v.isdigit():
            behind = int(v)
    return ahead, behind


def parse_ahead_behind_atom(text: str) -> tuple[int, int] | None:
    """`%(ahead-behind:<base>)` -> 'A B' (ahead of base, behind base)."""
    parts = text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def parse_log_no_walk(text: str) -> dict[str, tuple[int | None, str]]:
    """`git log --no-walk=unsorted --format=%H%x00%ct%x00%s <sha>...` -> {sha: (ct, subject)}."""
    out: dict[str, tuple[int | None, str]] = {}
    for line in text.split("\n"):
        if not line:
            continue
        sha, _, rest = line.partition("\0")
        ct, _, subj = rest.partition("\0")
        out[sha] = (int(ct) if ct.isdigit() else None, subj)
    return out


# --------------------------------------------------------------------------- fetch

_GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def _git(args: list[str], cwd: str, timeout: float):
    return ex.run(["git", "-c", "core.quotepath=off", "--no-optional-locks", *args], cwd=cwd,
                  timeout=timeout, env=_GIT_ENV)


def _short_branch(ref: str | None) -> str | None:
    if not ref:
        return None
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def _default_ref(root: str, timeout: float) -> str | None:
    """origin/HEAD target; reads the loose symref file directly (no process), falls back to git."""
    try:
        with open(os.path.join(root, ".git", "refs", "remotes", "origin", "HEAD"), encoding="utf-8") as fh:
            line = fh.readline().strip()
        if line.startswith("ref: refs/remotes/"):
            return line[len("ref: refs/remotes/"):]
    except OSError:
        pass
    if not os.path.exists(os.path.join(root, ".git", "reftable")):
        return None  # files backend and no loose symref => origin/HEAD unset
    r = _git(["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], root, timeout)
    ref = r.stdout.strip() if r.ok else ""
    return ref[len("refs/remotes/"):] if ref.startswith("refs/remotes/") else (ref or None)


class _RepoInfo(NamedTuple):
    default_ref: str | None
    worktrees: tuple[Worktree, ...]   # without dirty counts
    error: str | None


def _repo_info(root: str, timeout: float, now: float) -> _RepoInfo:
    default_ref = _default_ref(root, timeout)
    wl = _git(["worktree", "list", "--porcelain", "-z"], root, timeout)
    if not wl.ok:
        err = "timed out" if wl.timed_out else (wl.stderr.strip()[:200] or f"exit {wl.code}")
        return _RepoInfo(default_ref, (Worktree(root, root, None, None, True),), err)
    recs = [r for r in parse_worktree_porcelain_z(wl.stdout) if "worktree" in r and not r.get("bare")]

    branches = [r["branch"] for r in recs if r.get("branch")]
    refs: dict[str, dict] = {}
    if branches:
        fr = _git(["for-each-ref", f"--format={for_each_ref_format(default_ref)}", *branches], root, timeout)
        if fr.ok:
            refs = parse_for_each_ref(fr.stdout)
    detached = [r["HEAD"] for r in recs if not r.get("branch") and r.get("HEAD")]
    logs: dict[str, tuple[int | None, str]] = {}
    if detached:
        lr = _git(["log", "--no-walk=unsorted", "--format=%H%x00%ct%x00%s", *dict.fromkeys(detached)], root, timeout)
        if lr.ok:
            logs = parse_log_no_walk(lr.stdout)

    out: list[Worktree] = []
    for r in recs:
        path = os.path.realpath(r["worktree"])
        head = r.get("HEAD") or None
        kw: dict = dict(path=path, repo_root=root, branch=_short_branch(r.get("branch")), head=head,
                        is_root=(path == root), detached=bool(r.get("detached", False)),
                        prunable="prunable" in r, locked="locked" in r)
        ref = refs.get(r.get("branch") or "")
        ct: int | None = None
        if ref is not None:
            kw["last_commit"] = ref["subject"]
            ct = int(ref["ct"]) if ref["ct"].isdigit() else None
            ab = parse_track(ref["track"]) if ref["upstream"] else None
            if ab is not None:
                kw["base_ref"] = ref["upstream"]
            elif default_ref:
                ab = parse_ahead_behind_atom(ref["ab_default"])
                if ab is not None:
                    kw["base_ref"] = default_ref
            if ab is not None:
                kw["ahead"], kw["behind"] = ab
        elif head and head in logs:
            ct, kw["last_commit"] = logs[head]
            if default_ref:
                rl = _git(["rev-list", "--left-right", "--count", f"{default_ref}...{head}"], root, timeout)
                ab = parse_rev_list_count(rl.stdout) if rl.ok else None
                if ab is not None:
                    kw["ahead"], kw["behind"] = ab
                    kw["base_ref"] = default_ref
        if ct is not None:
            kw["last_commit_age_s"] = max(0, int(now - ct))
        out.append(Worktree(**kw))
    out.sort(key=lambda w: (not w.is_root, w.path))
    return _RepoInfo(default_ref, tuple(out), None)


def _index_size(path: str) -> int:
    """Size of the worktree's index file: a cheap proxy for `git status` cost."""
    dotgit = os.path.join(path, ".git")
    try:
        if os.path.isdir(dotgit):
            return os.path.getsize(os.path.join(dotgit, "index"))
        with open(dotgit, encoding="utf-8") as fh:
            line = fh.readline().strip()
        if line.startswith("gitdir: "):
            gd = line[len("gitdir: "):]
            if not os.path.isabs(gd):
                gd = os.path.join(path, gd)
            return os.path.getsize(os.path.join(gd, "index"))
    except OSError:
        pass
    return 1 << 30


def _dirty(path: str, timeout: float) -> int | None:
    r = _git(["status", "--porcelain=v2", "--untracked-files=normal"], path, timeout)
    return parse_status_v2(r.stdout).dirty if r.ok else None


def fetch(globs: list[str], timeout: float = 1.0, budget_s: float | None = None
          ) -> tuple[tuple[Repo, ...], list[SourceWarning]]:
    """Inventory all repos matched by `globs`.

    Phase 1 (one job per repo, ~3 processes): worktree list, branch/upstream/ahead-behind/last
    commit via one `for-each-ref`, detached HEADs via one `log --no-walk`.
    Phase 2 (one job per worktree, STATUS_WORKERS parallel): `status --porcelain=v2` for the dirty
    count - the expensive part (untracked scan; ~0.25 s per worktree of a large repo, ~1.3 s for
    all of ~/Projects). Both phases share `budget_s` (default: `timeout`); work not finished by
    the deadline is dropped: repos keep their phase-1 data, worktrees get dirty=None, and one
    warning per affected repo is emitted. Never blocks longer than ~budget_s.
    """
    if budget_s is None:
        budget_s = timeout
    t0 = time.monotonic()
    deadline = t0 + budget_s
    now = time.time()
    warnings: list[SourceWarning] = []
    roots = discover_repos(globs)
    if not roots:
        return (), warnings
    # Phase 1: one job per repo. Daemon threads only (see _par): abandoned work is bounded by
    # the per-process timeouts and never delays interpreter exit.
    def info_job(root: str):
        return lambda: _repo_info(root, max(0.05, min(timeout, deadline - time.monotonic())), now)

    info_res = _par.run_all([info_job(r) for r in roots], workers=MAX_WORKERS, deadline=deadline, name="agentree-git")
    infos: dict[str, _RepoInfo] = {}
    for root, res in zip(roots, info_res):
        if res[0] == _par.OK:
            infos[root] = res[1]
            if infos[root].error:
                warnings.append(SourceWarning(SOURCE, f"{os.path.basename(root)}: worktree list failed: {infos[root].error}"))
        elif res[0] == _par.ERR:
            warnings.append(SourceWarning(SOURCE, f"{os.path.basename(root)}: failed reading repo: {res[1]}"))
        else:
            warnings.append(SourceWarning(SOURCE, f"{os.path.basename(root)}: timed out reading repo"))

    # Phase 2: root checkouts first (most visible), then linked worktrees; cheapest (smallest
    # index) first within each group so the most results land before the deadline.
    jobs: list[tuple[bool, int, str]] = []
    for info in infos.values():
        for w in info.worktrees:
            if w.prunable or not os.path.isdir(w.path):
                continue
            jobs.append((not w.is_root, _index_size(w.path), w.path))
    jobs.sort()

    def dirty_job(path: str):
        # killed at the deadline at the latest
        return lambda: _dirty(path, deadline - time.monotonic()) if deadline - time.monotonic() > 0.05 else None

    paths = [p for _, _, p in jobs]
    dirty_res = _par.run_all([dirty_job(p) for p in paths], workers=STATUS_WORKERS,
                             deadline=deadline + 0.1, name="agentree-git-status")
    dirty: dict[str, int | None] = {p: _par.value(r) for p, r in zip(paths, dirty_res)}

    repos: list[Repo] = []
    for root in roots:
        info = infos.get(root)
        if info is None:
            repos.append(Repo(root, os.path.basename(root), None, (Worktree(root, root, None, None, True),)))
            continue
        wts = []
        missing = 0
        for w in info.worktrees:
            d = dirty.get(w.path)
            if d is None and not w.prunable and os.path.isdir(w.path):
                missing += 1
            wts.append(replace(w, dirty=d))
        if missing:
            warnings.append(SourceWarning(SOURCE, f"{os.path.basename(root)}: dirty state unknown for {missing} worktree(s) (git status slow)"))
        repos.append(Repo(root, os.path.basename(root), info.default_ref, tuple(wts)))
    return tuple(repos), warnings
