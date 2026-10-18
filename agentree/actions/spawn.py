"""Spawn: worktree + tab (or session) + optional agent (any registered provider), with rollback.

plan_spawn is cheap and side-effect free (filesystem checks + a few fast read-only git commands,
origin/HEAD cached); execute_spawn runs the planned steps in order with ProgressEvents, checks
`cancel` between steps and during the install, and on failure/cancel rolls back in reverse what
*this run* created:

  tab      close-tab-by-id, only for the tab ensure_tab created and verified
  session  kill-session + delete-session, only if it did not exist before our launch
  worktree `git worktree remove --force`, only while pristine: HEAD still the commit it was
           created at, `git status` clean apart from the .env files we copied
  branch   `git update-ref -d refs/heads/<b> <created-at commit>` (atomic compare-and-delete),
           only if this run created it (`worktree add -b`)
The `.worktrees/` exclude line is left in place (harmless).

zellij 0.45.1 (verified live): session names cannot contain '/', so a new session for
<repo>/<slug> is named `<repo>-<slug>`; a new session is always created *with* a client (a kitty
tab running `zellij -s <name> [-n <layout>]`), never client-less. The prompt reaches the agent
through a generated layout: the provider's launch argv (build_launch(mode="spawn"), e.g.
`args "--prompt" "<kdl-escaped>"`; verified to arrive intact with quotes, backslashes and
newlines). A provider that generates no argv (Launch.argv None) keeps the named layout. Provider
setup steps (e.g. a hooks settings file) run right before the tab/session.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agentree.actions.api import ActionResult, Plan, ProgressEvent, SpawnOptions, SpawnRequest
from agentree.actions.jump import Navigator, Target, _Fail, deepest, find_session
from agentree.actions.runner import Runner, canonical_dir
from agentree.types import Row, SessionNode, Snapshot, WorktreeNode, ZSession

INSTALL_TIMEOUT_S = 600.0
SESSION_WAIT_S = 6.0
AGENT_WAIT_S = 3.0
SKIP_ENV = {".env.example", ".env.sample", ".env.template"}
EXCLUDE_BEGIN = "# BEGIN agentree worktrees"
EXCLUDE_END = "# END agentree worktrees"


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- pure helpers

def kdl_str(s: str) -> str:
    """A KDL (v1) string literal for any text."""
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append(f"\\u{{{ord(ch):x}}}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def agent_layout(argv: tuple[str, ...] | list[str], *, pane_name: str) -> str:
    """The agent tab (mirrors ~/.config/zellij/layouts/agent.kdl: agent 65% | shell), one line:
    the provider's launch argv escaped into KDL (`command` = argv[0], `args` = the rest)."""
    argv = list(argv) or [""]
    args = argv[1:]
    a = f" {{ args {' '.join(kdl_str(x) for x in args)}; }}" if args else ""
    return ('layout { pane split_direction="vertical" { '
            f'pane size="65%" name={kdl_str(pane_name)} command={kdl_str(argv[0])} focus=true{a}; '
            'pane size="35%"; }; }')


PROMPT_PLACEHOLDER = "<prompt>"


def _display_argv(argv: tuple[str, ...], prompt: bool) -> str:
    """The launch argv for previews: shell-quoted, the prompt shown as a bare <prompt>."""
    return " ".join(PROMPT_PLACEHOLDER if prompt and x == PROMPT_PLACEHOLDER else shlex.quote(x) for x in argv)


def slug_of(branch: str) -> str:
    s = branch.strip().rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")


def session_name_for(repo: str, slug: str) -> str:
    """`<repo>-<slug>`: zellij rejects '/' in session names (socket path)."""
    name = f"{os.path.basename(repo.rstrip('/'))}-{slug}"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)


def env_files(repo: str) -> list[str]:
    """Top-level `.env*` regular files of the root checkout (examples/samples excluded)."""
    try:
        names = sorted(os.listdir(repo))
    except OSError:
        return []
    return [n for n in names if n.startswith(".env") and n not in SKIP_ENV
            and os.path.isfile(os.path.join(repo, n)) and not os.path.islink(os.path.join(repo, n))]


def detect_install(repo: str, install_commands: dict[str, str]) -> tuple[str, str] | None:
    """(lockfile, command) for the first lockfile present, in config order."""
    for lock, cmd in (install_commands or {}).items():
        if cmd and os.path.isfile(os.path.join(repo, lock)):
            return lock, cmd
    return None


def repo_of_row(row: Row | None, snap: Snapshot) -> str | None:
    if row is None:
        return None
    ref = row.ref
    if isinstance(ref, WorktreeNode):
        return ref.worktree.repo_root
    if isinstance(ref, SessionNode):
        return next((wn.worktree.repo_root for wn in ref.worktrees), None)
    cwd = getattr(ref, "cwd", None)
    if cwd:
        wts = [wn.worktree for sn in snap.sessions for wn in sn.worktrees] + list(snap.orphans.worktrees)
        hit = deepest(cwd, {w.path for w in wts})
        return next((w.repo_root for w in wts if w.path == hit), None)
    return None


def repo_sessions(snap: Snapshot, repo: str) -> list[SessionNode]:
    """Live sessions holding a worktree of `repo`."""
    out = []
    for sn in snap.sessions:
        if sn.session.state == "exited":
            continue
        if any(wn.worktree.repo_root == repo or wn.worktree.path == repo for wn in sn.worktrees):
            out.append(sn)
    return out


def choose_session(snap: Snapshot, repo: str, current: str | None) -> SessionNode | str:
    cands = repo_sessions(snap, repo)
    name = os.path.basename(repo.rstrip("/"))
    if not cands:
        return f"no live session for {name}; choose new session"
    if current and any(c.session.name == current for c in cands):
        return next(c for c in cands if c.session.name == current)
    if len(cands) == 1:
        return cands[0]
    # the "repo session": the one with the root checkout open (spawned sessions hold one worktree)
    rooted = [c for c in cands if any(wn.worktree.path == repo and wn.tabs for wn in c.worktrees)]
    if len(rooted) == 1:
        return rooted[0]
    named = [c for c in cands if c.session.name == name]
    if len(named) == 1:
        return named[0]
    return (f"{name} is open in several sessions ({', '.join(c.session.name for c in cands)}); "
            f"run agentree inside the one you want, or choose new session")


# --------------------------------------------------------------------------- git (read-only)

_default_cache: dict[str, tuple[str | None, float]] = {}
_cache_lock = threading.Lock()
DEFAULT_TTL_S = 30.0


def default_ref(repo: str) -> str | None:
    """origin/HEAD target (e.g. origin/main), cached per repo."""
    now = time.monotonic()
    with _cache_lock:
        hit = _default_cache.get(repo)
        if hit and now - hit[1] < DEFAULT_TTL_S:
            return hit[0]
    from agentree.sources import git as gsrc

    ref = gsrc._default_ref(repo, 1.0)
    with _cache_lock:
        _default_cache[repo] = (ref, now)
    return ref


def _git(runner: Runner, *args: str, timeout: float = 2.0):
    return runner.query(["git", *args], timeout=timeout)


def worktree_branches(runner: Runner, repo: str) -> dict[str, str]:
    """{refs/heads/<b>: worktree path} for branches checked out in any worktree."""
    from agentree.sources import git as gsrc

    r = _git(runner, "-C", repo, "worktree", "list", "--porcelain", "-z")
    if not r.ok:
        return {}
    return {rec["branch"]: rec.get("worktree", "") for rec in gsrc.parse_worktree_porcelain_z(r.stdout)
            if rec.get("branch")}


# --------------------------------------------------------------------------- options / plan

def spawn_options(row: Row | None, snap: Snapshot, cfg: Any) -> SpawnOptions:
    from agentree.sources import git as gsrc

    repos = tuple(gsrc.discover_repos(list(cfg.expanded_repo_roots())))
    want = repo_of_row(row, snap)
    from agentree.actions.agent import agent_choices
    from agentree.providers import registry

    return SpawnOptions(repos=repos, default_repo=want if want in repos else None,
                        layouts=tuple(getattr(cfg, "layouts", ()) or ()),
                        branch_prefix=str(getattr(cfg, "branch_prefix", "") or ""),
                        agents=agent_choices(cfg),
                        default_provider=registry.default_id(cfg))


@dataclass
class _P:
    steps: list[tuple[str, ...]] = field(default_factory=list)
    spec: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, display: tuple[str, ...], **spec: Any) -> None:
        self.steps.append(tuple(display))
        self.spec.append(spec)


def plan_spawn(req: SpawnRequest, snap: Snapshot, *, cfg: Any, nav: Navigator, state_dir: str) -> Plan:
    p = _P()
    data: dict[str, Any] = {"target": req.target, "switch": bool(req.switch_after), "taken_at": snap.taken_at}

    def done() -> Plan:
        data["spec"] = p.spec
        return Plan(kind="spawn", confirm=None, steps=tuple(p.steps), data=data,
                    warnings=tuple(p.warnings), errors=tuple(p.errors))

    repo = os.path.realpath(os.path.expanduser(req.repo or ""))
    if not req.repo or not os.path.isdir(os.path.join(repo, ".git")):
        p.errors.append(f"repo {req.repo or '?'} not found (needs a main checkout with .git/)")
        return done()
    data["repo"] = repo
    branch = (req.branch or "").strip()
    slug = slug_of(branch)
    data.update(branch=branch, slug=slug)
    det = detect_install(repo, getattr(cfg, "install_commands", {}) or {})
    data["install_cmd"] = det[1] if det else None
    if not branch:
        p.errors.append("type a branch name")
        return done()
    if not slug:
        p.errors.append(f"cannot derive a directory name from {branch!r}")
        return done()
    path = os.path.realpath(cfg.worktree_path(repo, slug))
    data["worktree_path"] = path
    run = nav.runner
    ref = f"refs/heads/{branch}"
    base = "HEAD"
    if req.base == "origin":
        base = default_ref(repo) or "HEAD"
        if base == "HEAD":
            p.warnings.append("no origin/HEAD in this repo: branching from HEAD")
    rel = os.path.relpath(path, repo)
    inside = not rel.startswith("..")
    # independent read-only checks, in parallel (each git start costs ~8 ms)
    from agentree.sources import _par

    envs = env_files(repo) if req.copy_env else []
    checks = [lambda: _git(run, "check-ref-format", "--branch", branch),
              lambda: _git(run, "-C", repo, "show-ref", "--verify", "--quiet", ref),
              lambda: _git(run, "-C", repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"),
              (lambda: _git(run, "-C", repo, "check-ignore", "-q", rel)) if inside else (lambda: None),
              # .env names tracked in what will be checked out: never overwritten, so predict skips
              (lambda: _git(run, "-C", repo, "ls-tree", "--name-only", "-z", ref if _branch_exists() else base,
                            "--", *envs)) if envs else (lambda: None)]

    def _branch_exists() -> bool:
        return _git(run, "-C", repo, "show-ref", "--verify", "--quiet", ref).ok
    res = [_par.value(o) for o in _par.run_all(checks, deadline=time.monotonic() + 3.0)]
    if res[0] is None or not res[0].ok:
        p.errors.append(f"invalid branch name {branch!r}")
        return done()
    if os.path.lexists(path):
        p.errors.append(f"{path} already exists")
    exists = res[1] is not None and res[1].ok
    data["created_branch"] = not exists
    if exists:
        where = worktree_branches(run, repo).get(ref)
        if where:
            p.errors.append(f"branch {branch} is already checked out at {where}")
        else:
            p.warnings.append(f"branch {branch} already exists: reusing it (no new branch)")
        add_argv = ("git", "-C", repo, "worktree", "add", path, branch)
        data["base_ref"] = branch
        p.warnings = [w for w in p.warnings if "origin/HEAD" not in w]
    else:
        if res[2] is None or not res[2].ok:
            p.errors.append(f"base {base} not found")
        data["base_ref"] = base
        add_argv = ("git", "-C", repo, "worktree", "add", "-b", branch, path, base)

    # target
    name = slug
    layout_string = None
    from agentree.actions.agent import get_provider, provider_available
    from agentree.providers import registry
    from agentree.providers.base import LaunchRequest

    pid = req.agent
    data["provider"] = pid
    data["launch_session_id"] = None
    try:
        prov = get_provider(pid)
    except _Fail as e:
        p.errors.append(str(e))
        return done()
    # the provider decides whether this request starts the agent (a prompt always does, in the
    # built-in agent layout; argv None keeps the named layout); the session id, if any, is
    # generated here, at plan time, and never again at execute time
    lr = LaunchRequest(mode="spawn", prompt=req.prompt or "", layout=req.layout, state_dir=state_dir)
    launch = prov.build_launch(cfg, lr)
    setup: tuple = ()
    agent_desc = ""
    p.errors.extend(launch.errors)
    p.warnings.extend(launch.warnings)
    if launch.argv is not None:
        if not provider_available(prov, cfg):
            msg = registry.missing_command_message(pid, cfg)
            if msg not in p.errors:
                p.errors.append(msg)
        data["launch_session_id"] = launch.session_id
        setup = launch.setup
        layout_string = agent_layout(launch.argv, pane_name=pid)
        shown = prov.build_launch(cfg, dataclasses.replace(
            lr, prompt=PROMPT_PLACEHOLDER if req.prompt else "", session_id=launch.session_id))
        agent_desc = _display_argv(tuple(shown.argv if shown.argv is not None else launch.argv), bool(req.prompt))
        if req.layout and req.layout != "agent":
            p.warnings.append(f"a prompt uses the built-in agent layout instead of {req.layout!r}")
    data["agent_cmd"] = agent_desc or None
    session_node: SessionNode | None = None
    if req.target == "session":
        sname = session_name_for(repo, slug)
        data["session"] = sname
        if find_session(snap, sname) is not None:
            p.errors.append(f"session {sname} already exists")
        sdir = nav.env.exited_socket_dir()
        data["socket_dir"] = sdir
        try:
            sock = nav.launch_socket()
        except _Fail as e:
            p.errors.append(str(e))
            sock = "unix:/tmp/kitty.sock-?"
        data["kitty_socket"] = sock
        layout_file = None
        if layout_string:   # unique name; created O_EXCL 0600 at execute, deleted once the session runs
            import secrets
            layout_file = os.path.join(state_dir, "layouts", f"spawn-{sname}-{secrets.token_hex(4)}.kdl")
        data["layout_file"] = layout_file
    else:
        ch = choose_session(snap, repo, nav.env.current_session)
        if isinstance(ch, str):
            p.errors.append(ch)
        else:
            session_node = ch
            if not ch.session.inventory_complete:
                p.errors.append(f"{ch.session.name}: tab/pane inventory incomplete — refusing")
            data["session"] = ch.session.name
            data["socket_dir"] = ch.session.socket_dir

    # steps (display = exact commands)
    if inside:
        if not (res[3] is not None and res[3].ok):
            pattern = os.path.dirname(rel).rstrip("/") + "/"
            exclude = os.path.join(repo, ".git", "info", "exclude")
            p.add((f"append {pattern} to {exclude}",), kind="exclude", file=exclude, pattern=pattern)
    p.add(add_argv, kind="worktree", argv=list(add_argv))
    if req.copy_env:
        tracked = set(filter(None, (res[4].stdout if res[4] is not None and res[4].ok else "").split("\0")))
        files = [f for f in envs if f not in tracked]
        for f in sorted(tracked):
            p.warnings.append(f"{f} is tracked in {branch if exists else base}: not copied (never overwritten)")
        if files:
            p.add((f"copy .env files (no overwrite, no symlinks): {', '.join(files)} → {path}/",),
                  kind="env", files=files)
        elif not envs:
            p.warnings.append("no .env files in the repo root to copy")
    if req.run_install:
        if det:
            p.add((f"cd {shlex.quote(path)} && {det[1]}",), kind="install", argv=shlex.split(det[1]), cwd=path)
            p.warnings.append("install may download packages from the network")
        else:
            p.warnings.append("no known lockfile in the repo root: install skipped")
    for su in setup:
        p.add((su.label,), kind="provider_setup", provider=pid, operation=su.operation, label=su.label,
              payload=[[k, v] for k, v in su.payload])
    if req.target == "session":
        sname, sdir, sock = data["session"], data["socket_dir"], data["kitty_socket"]
        zargs = [nav.zellij, "-s", sname]
        if data["layout_file"]:
            p.add((f"write the agent layout ({agent_desc}) to {data['layout_file']} (0600, removed after start)",),
                  kind="layout_file", file=data["layout_file"], content=layout_string)
            zargs += ["-n", data["layout_file"]]
        elif req.layout:
            zargs += ["-n", req.layout]
        argv = [nav.kitty, "@", "--to", sock, "launch", "--type=tab", "--cwd", path, "--tab-title", sname,
                "--var", f"agentree_session={sname}", "--env", f"ZELLIJ_SOCKET_DIR={sdir}"]
        if not req.switch_after:
            argv.append("--keep-focus")
        argv += zargs
        p.add(tuple(argv), kind="session", argv=argv, name=sname, socket_dir=sdir, sock=sock)
    elif session_node is not None:
        s = session_node.session
        args = ["new-tab", "--cwd", path, "--name", name]
        if layout_string:
            args += ["--layout-string", layout_string]
        elif req.layout:
            args += ["--layout", req.layout]
        if not req.switch_after:
            args.append("--no-focus")
        argv, _env = nav.zj(s, *args)
        p.add(tuple(argv), kind="tab", session=s.name, socket_dir=s.socket_dir, name=name,
              layout=req.layout, layout_string=layout_string, age=s.created_age_s)
    p.add(("record it in links.json",), kind="links")
    if req.switch_after:
        p.add((f"switch to {data.get('session', '?')} › {name}",), kind="switch")
    return done()


# --------------------------------------------------------------------------- journal

class Journal:
    """What this run is about to create / has created, with observed identities.

    Each create step appends an entry (state "pending") *before* acting and updates it after
    ("created" / "absent"). Persisted to <state>/journal/<run-id>.json while the run lasts and
    deleted at the end, so a crash leaves a trace (`state.list_journals()`, `agentree doctor`)."""

    def __init__(self, run_id: str, meta: dict, persist: bool, store=None) -> None:
        self.run_id = run_id
        self.meta = meta
        self.entries: list[dict] = []
        self.persist = persist
        if store is None:
            from agentree import state as store  # type: ignore[no-redef]
        self.store = store

    unsaved = False

    def save(self, strict: bool = True) -> None:
        """Persist. strict (every create path): a write failure raises _Fail, so nothing is created
        that the on-disk journal does not know about. Rollback saves are best effort."""
        if not self.persist:
            return
        try:
            self.store.write_journal(self.run_id, {"run_id": self.run_id, **self.meta, "entries": self.entries})
            self.unsaved = False
        except OSError as e:
            self.unsaved = True
            if strict:
                raise _Fail(f"journal not writable ({e}) — stopping before creating anything more") from None

    def begin(self, kind: str, step: int, **ident: Any) -> dict:
        e = {"kind": kind, "step": step, "state": "pending", "at": time.time(), **ident}
        self.entries.append(e)
        try:
            self.save()
        except _Fail:
            self.entries.remove(e)       # not persisted -> the caller does not create it
            raise
        return e

    def update(self, e: dict, state: str | None = None, strict: bool = True, **obs: Any) -> None:
        e.update(obs)
        if state:
            e["state"] = state
        self.save(strict)

    def close(self) -> None:
        if self.persist:
            try:
                self.store.delete_journal(self.run_id)
            except OSError:
                pass


# --------------------------------------------------------------------------- file fingerprints

FP_MAX_ENTRIES = 20000
FP_MAX_S = 2.0


def _sha256_file(path: str) -> str | None:
    import hashlib

    h = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            h.update(b)
    except OSError:
        return None
    finally:
        os.close(fd)
    return h.hexdigest()


def _fingerprint_dir(root: str) -> str | None:
    """Hash of the sorted (relpath, type, size, mtime_ns, sha256 | link target) of everything below
    `root`, walked without following symlinks. None (unknown) past FP_MAX_ENTRIES / FP_MAX_S, on
    any walk error, or for any entry that is not a dir, regular file or symlink."""
    import hashlib
    import json
    import stat as st_

    deadline = time.monotonic() + FP_MAX_S
    rows: list[tuple] = []
    errors: list[OSError] = []
    try:
        rst = os.lstat(root)
    except OSError:
        return None
    rows.append((".", "d", 0, rst.st_mtime_ns, ""))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=errors.append):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                st = os.lstat(full)
            except OSError:
                return None
            if st_.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(full)
                except OSError:
                    return None
                rows.append((rel, "l", 0, st.st_mtime_ns, target))
            elif st_.S_ISDIR(st.st_mode):
                rows.append((rel, "d", 0, st.st_mtime_ns, ""))
            elif st_.S_ISREG(st.st_mode):
                sha = _sha256_file(full)
                if sha is None:
                    return None
                rows.append((rel, "f", st.st_size, st.st_mtime_ns, sha))
            else:
                return None                      # fifo, socket, device: unknown
            if len(rows) > FP_MAX_ENTRIES or time.monotonic() > deadline:
                return None
    if errors:
        return None
    return "dir:" + hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def fingerprint(path: str) -> str | None:
    """sha256 of a regular file's bytes, `link:<target>` for a symlink, a recursive digest for a
    directory (see _fingerprint_dir); None = unknown (missing, other type, walk cap exceeded).
    Unknown never matches anything, not even another unknown."""
    import stat as st_

    try:
        st = os.lstat(path.rstrip("/") or path)
    except OSError:
        return None
    p = path.rstrip("/") or path
    if st_.S_ISLNK(st.st_mode):
        try:
            return "link:" + os.readlink(p)
        except OSError:
            return None
    if st_.S_ISDIR(st.st_mode):
        return _fingerprint_dir(p)
    if st_.S_ISREG(st.st_mode):
        return _sha256_file(p)
    return None


BASELINE_MAX = 2000


def status_entries(run: Runner, path: str) -> tuple[bool, list[str], list[str]] | None:
    """(tracked changes?, untracked files, ignored files) — per file, ignored ones included."""
    r = run.query(["git", "-C", path, "status", "--porcelain=v2", "-z", "--ignored=traditional", "-uall"],
                  timeout=30.0)
    if not r.ok:
        return None
    tracked, untracked, ignored = False, [], []
    parts = r.stdout.split("\0")
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        if rec[:2] in ("1 ", "u "):
            tracked = True
        elif rec[:2] == "2 ":
            tracked = True
            i += 1                       # rename/copy: the original path follows
        elif rec[:2] == "? ":
            untracked.append(rec[2:])
        elif rec[:2] == "! ":
            ignored.append(rec[2:])
    return tracked, untracked, ignored


def baseline(run: Runner, path: str) -> dict[str, str] | None:
    """Untracked + ignored files right after `worktree add` (e.g. written by hooks), fingerprinted."""
    st = status_entries(run, path)
    if st is None:
        return None
    files = st[1] + st[2]
    if len(files) > BASELINE_MAX:
        return None
    return {f: fingerprint(os.path.join(path, f)) for f in files}


def pristine(run: Runner, path: str, head: str | None, base_files: dict[str, str] | None,
             copied: dict[str, str]) -> tuple[bool, str]:
    """Only files agentree created: tracked files unchanged, HEAD unchanged, and every untracked or
    ignored file either a copied .env file or part of the post-creation baseline, with its
    fingerprint unchanged."""
    if head is None or base_files is None:
        return False, "creation state unknown"
    h = run.query(["git", "-C", path, "rev-parse", "HEAD"])
    if not h.ok or h.stdout.strip() != head:
        return False, "HEAD moved"
    st = status_entries(run, path)
    if st is None:
        return False, "git status failed"
    tracked, untracked, ignored = st
    if tracked:
        return False, "tracked files changed"
    for f in untracked + ignored:
        want = copied.get(f, base_files.get(f))
        now = fingerprint(os.path.join(path, f))
        if want is None or now is None or now != want:
            return False, "contains files not created by agentree"
    return True, ""


def copy_env_files(repo: str, path: str, names: list[str]) -> tuple[dict[str, str], list[str]]:
    """Copy top-level .env files from the root checkout into the new worktree, in-process:
    the source must be a regular file (not a symlink), the destination must not exist (tracked
    file, symlink, anything) and is created O_CREAT|O_EXCL|O_NOFOLLOW 0600; mtime preserved.
    Returns ({name: sha256}, skip notes)."""
    import hashlib
    import stat as st_

    copied: dict[str, str] = {}
    skipped: list[str] = []
    for name in names:
        src, dst = os.path.join(repo, name), os.path.join(path, name)
        try:
            sst = os.lstat(src)
        except FileNotFoundError:
            skipped.append(f"{name}: gone from the root checkout")
            continue
        if not st_.S_ISREG(sst.st_mode):
            skipped.append(f"{name}: not a regular file in the root checkout")
            continue
        try:
            os.lstat(dst)
            skipped.append(f"{name}: already in the worktree (tracked or symlink) — not overwritten")
            continue
        except FileNotFoundError:
            pass
        sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            chunks = []
            while True:
                b = os.read(sfd, 1 << 20)
                if not b:
                    break
                chunks.append(b)
        finally:
            os.close(sfd)
        data = b"".join(chunks)
        try:
            dfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            skipped.append(f"{name}: appeared in the worktree — not overwritten")
            continue
        try:
            view = memoryview(data)
            while view:
                n = os.write(dfd, view)
                view = view[n:]
        finally:
            os.close(dfd)
        os.utime(dst, ns=(sst.st_atime_ns, sst.st_mtime_ns), follow_symlinks=False)
        copied[name] = hashlib.sha256(data).hexdigest()
    return copied, skipped


# --------------------------------------------------------------------------- execute

@dataclass
class _Made:
    tab_id: int | None = None
    tab_name: str | None = None
    zsession: ZSession | None = None
    window: tuple[str, int] | None = None


def display(step: tuple[str, ...]) -> str:
    return shlex.join(step) if len(step) > 1 else step[0]


def execute_spawn(plan: Plan, nav: Navigator, *, cfg: Any, state_dir: str,
                  progress: Callable[[ProgressEvent], None] | None = None,
                  cancel: threading.Event | None = None,
                  find_agents: Callable[[str, set[int]], list[int]] | None = None,
                  links=None, journal_store=None) -> ActionResult:
    from agentree.actions.tabs import named_lock, repo_lock_path

    if plan.errors:
        return ActionResult(False, plan.errors[0])
    d = plan.data
    try:
        with named_lock(repo_lock_path(state_dir, d["repo"]), busy="another agentree is changing this repo — try again"):
            return _execute(plan, nav, cfg=cfg, state_dir=state_dir, progress=progress, cancel=cancel,
                            find_agents=find_agents, links=links, journal_store=journal_store)
    except _Fail as e:
        return ActionResult(False, str(e))


def _execute(plan: Plan, nav: Navigator, *, cfg, state_dir, progress, cancel, find_agents, links,
             journal_store) -> ActionResult:
    import secrets

    from agentree.actions.tabs import named_lock, session_name_lock_path

    d = plan.data
    spec: list[dict] = d["spec"]
    total = len(spec)
    run = nav.runner
    dry = run.dry_run
    made = _Made()
    labels = [display(s) for s in plan.steps]
    if find_agents is None:
        from agentree.actions.agent import get_provider, pid_finder

        # plans with no provider predate providers: opencode (compat default)
        find_agents = pid_finder(get_provider(d.get("provider") or "opencode"), d.get("launch_session_id"))
    if links is None:
        from agentree import state as links  # type: ignore[no-redef]
    cancel = cancel or threading.Event()
    notes: list[str] = []
    path, repo = d["worktree_path"], d["repo"]
    jr = Journal(f"spawn-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}",
                 {"repo": repo, "branch": d["branch"], "worktree": path, "session": d.get("session"),
                  "pid": os.getpid()}, persist=not dry, store=journal_store)

    def ev(i: int, state: str, detail: str = "", label: str | None = None) -> None:
        if progress is not None:
            try:
                progress(ProgressEvent(i, total, label or labels[i], state, detail))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 - the UI's problem, not ours
                pass

    def err(r) -> str:
        lines = ((r.stderr or "") + "\n" + (r.stdout or "")).strip().splitlines()
        return (lines[-1] if lines else f"exit {r.code}")[:200]

    def rev(*args: str) -> str | None:
        r = run.query(["git", *args])
        return r.stdout.strip() if r.ok and r.stdout.strip() else None

    # ------------------------------------------------------------- step handlers
    def do_exclude(i: int, st: dict) -> str:
        def write() -> None:
            os.makedirs(os.path.dirname(st["file"]), exist_ok=True)
            try:
                with open(st["file"], encoding="utf-8") as fh:
                    if st["pattern"] in fh.read().splitlines():
                        return
            except FileNotFoundError:
                pass
            with open(st["file"], "a", encoding="utf-8") as fh:
                fh.write(f"{EXCLUDE_BEGIN}\n{st['pattern']}\n{EXCLUDE_END}\n")
        run.internal(f"append {st['pattern']} to {st['file']}", write)
        return ""

    def do_worktree(i: int, st: dict) -> str:
        if os.path.lexists(path):
            raise _Fail(f"{path} appeared since the preview — refusing")
        ref = f"refs/heads/{d['branch']}"
        e_br = None
        if d["created_branch"]:
            if run.query(["git", "-C", repo, "show-ref", "--verify", "--quiet", ref]).ok:
                raise _Fail(f"branch {d['branch']} appeared since the preview — refusing")
            base_sha = None if dry else rev("-C", repo, "rev-parse", "--verify", "--quiet", f"{d['base_ref']}^{{commit}}")
            if base_sha is None and not dry:
                raise _Fail(f"base {d['base_ref']} not found")
            e_br = jr.begin("branch", i, name=d["branch"], sha=base_sha)
        e_wt = jr.begin("worktree", i, path=path)
        r = run.act(st["argv"], timeout=60.0)
        if dry:
            return ""
        if not r.ok:
            # a failed / timed-out add may still have created the branch and/or the worktree
            if e_br is not None:
                tip = rev("-C", repo, "rev-parse", "--verify", "--quiet", ref)
                jr.update(e_br, "created" if tip and tip == e_br["sha"] else ("foreign" if tip else "absent"))
            from agentree.sources import git as gsrc
            wl = run.query(["git", "-C", repo, "worktree", "list", "--porcelain", "-z"])
            reg = wl.ok and any(os.path.realpath(x.get("worktree", "")) == path
                                for x in gsrc.parse_worktree_porcelain_z(wl.stdout))
            if reg:
                jr.update(e_wt, "created", head=rev("-C", path, "rev-parse", "HEAD"), baseline=baseline(run, path))
            else:
                jr.update(e_wt, "absent")
            raise _Fail(f"worktree add: {err(r)}")
        head = rev("-C", path, "rev-parse", "HEAD")
        jr.update(e_wt, "created", head=head, baseline=baseline(run, path))
        if e_br is not None:
            jr.update(e_br, "created" if head == e_br["sha"] else "foreign")
        return ""

    def do_env(i: int, st: dict) -> str:
        e = jr.begin("env", i, files={})
        res: dict = {}

        def copy() -> None:
            res["copied"], res["skipped"] = copy_env_files(repo, path, st["files"])
        run.internal(f"copy .env files {', '.join(st['files'])} -> {path}/", copy)
        jr.update(e, "created", files=res.get("copied", {}))
        notes.extend(res.get("skipped", []))
        detail = ", ".join(res.get("copied", {}))
        if res.get("skipped"):
            detail += ("; " if detail else "") + "skipped " + "; ".join(res["skipped"])
        return detail

    def do_install(i: int, st: dict) -> str:
        r = run.stream(st["argv"], cwd=st["cwd"], timeout=INSTALL_TIMEOUT_S, cancel=cancel,
                       on_line=lambda line: ev(i, "running", line[-200:]))
        if getattr(r, "cancelled", False):
            raise _Cancelled()
        if not r.ok:
            raise _Fail(f"install failed ({'timed out' if r.timed_out else f'exit {r.code}'}): {err(r)}")
        return ""

    def do_tab(i: int, st: dict) -> str:
        from agentree.actions.tabs import ensure_tab

        name = st["session"]
        fresh = nav.fresh_session(name)
        if fresh.live_dir is None:
            raise _Fail(f"{name} is no longer live — refresh (R)")
        s = ZSession(name, "attached", st.get("age"), fresh.live_dir, 1)
        nav.check_incarnation(dataclasses.replace(s, socket_dir=st.get("socket_dir") or fresh.live_dir),
                              fresh, d.get("taken_at"))
        holder: dict = {}

        def before() -> None:
            holder["e"] = jr.begin("tab", i, session=name, socket_dir=fresh.live_dir, age=fresh.age_s,
                                   name=st["name"], cwd=path)

        def created(tid: int) -> None:          # persisted before any further zellij query
            jr.update(holder["e"], "created", tab_id=tid)

        res = ensure_tab(nav, s, path, st["name"], state_dir=state_dir, layout=st.get("layout"),
                         layout_string=st.get("layout_string"), roots={repo, path}, focus=d["switch"],
                         before_create=before, on_created=created)
        e = holder.get("e")
        if e is not None and res.tab_id is not None and e.get("tab_id") != res.tab_id:
            jr.update(e, "created", tab_id=res.tab_id)
        if not res.ok:
            raise _Fail(res.message)
        made.tab_id = res.tab_id
        made.tab_name = st["name"]
        made.zsession = dataclasses.replace(s, clients=res.clients or 1)
        return f"tab {res.tab_id}" if res.tab_id is not None else ""

    def do_provider_setup(i: int, st: dict) -> str:
        from agentree.actions.agent import get_provider
        from agentree.providers.base import SetupStep

        prov = get_provider(st.get("provider"))
        step = SetupStep(st["operation"], st.get("label") or st["operation"],
                         tuple((str(k), v) for k, v in st.get("payload") or ()))
        return prov.execute_setup(step, runner=run, state_dir=state_dir) or ""

    def do_layout_file(i: int, st: dict) -> str:
        e = jr.begin("layout_file", i, path=st["file"])

        def write() -> None:
            os.makedirs(os.path.dirname(st["file"]), mode=0o700, exist_ok=True)
            fd = os.open(st["file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(st["content"] + "\n")
            jr.update(e, "created", ino=os.lstat(st["file"]).st_ino)
        run.internal(f"write layout {st['file']} (0600)", write)
        return ""

    def drop_layout_file() -> None:
        for e in jr.entries:
            if e["kind"] == "layout_file" and e["state"] == "created":
                _unlink_if_same(e)

    def do_session(i: int, st: dict) -> str:
        name, sdir = st["name"], st["socket_dir"]
        with named_lock(session_name_lock_path(state_dir, name),
                        busy=f"another agentree is creating session {name} — try again"):
            if nav.lookup_session(name) is not None:
                raise _Fail(f"session {name} already exists — refusing")
            if not nav.owned(st["sock"]):
                raise _Fail(f"kitty socket {st['sock']} is gone or not owned — refusing")
            e_s = jr.begin("session", i, name=name, socket_dir=sdir)
            e_w = jr.begin("window", i, sock=st["sock"], session=name)
            r = run.act(st["argv"], timeout=5.0)
            out = r.stdout.strip()
            if out.isdigit():
                made.window = (st["sock"], int(out))
                jr.update(e_w, "created", window_id=int(out))
            if not r.ok:
                if not out.isdigit() and not r.timed_out:
                    # kitty refused the launch: no window, so no zellij process either
                    jr.update(e_w, "absent")
                    jr.update(e_s, "absent")
                raise _Fail(f"kitty launch: {err(r)}")
            if dry:
                return ""
            s = ZSession(name, "attached", None, sdir, 0)
            deadline = time.monotonic() + SESSION_WAIT_S
            while True:
                f = nav.lookup_session(name)
                if f is not None and f.live_dir is not None:
                    if canonical_dir(f.live_dir) != canonical_dir(sdir):
                        raise _Fail(f"session {name} came up in {f.live_dir}, expected {sdir}")
                    if e_s["state"] != "created":
                        jr.update(e_s, "created", socket_dir=f.live_dir, age=f.age_s, seen=time.time())
                    s = dataclasses.replace(s, socket_dir=f.live_dir)
                    if nav.clients(s) >= 1:
                        break
                if time.monotonic() >= deadline:
                    raise _Fail(f"session {name} did not come up with a client within {SESSION_WAIT_S:.0f}s")
                if cancel.is_set():
                    raise _Cancelled()
                nav.sleep(0.1)
        drop_layout_file()                       # zellij has read it
        deadline = time.monotonic() + 2.0
        while True:
            tabs, panes = nav.fresh_inventory(s)
            tid = next((p.tab_id for p in panes if not p.is_plugin and deepest(p.cwd, {repo, path}) == path), None)
            if tid is not None or time.monotonic() >= deadline:
                break
            nav.sleep(0.1)
        if tid is None:
            raise _Fail(f"session {name} has no pane in {path}")
        made.tab_id = tid
        made.tab_name = next((t.name for t in tabs if t.tab_id == tid), None)
        made.zsession = dataclasses.replace(s, clients=1)
        return f"session {name}"

    def do_links(i: int, st: dict) -> str:
        pids: list[int] = []
        s = made.zsession
        if not dry and s is not None and made.tab_id is not None and _has_agent():
            deadline = time.monotonic() + AGENT_WAIT_S
            while True:
                try:
                    _tabs, panes = nav.fresh_inventory(s)
                    ids = {p.pane_id for p in panes if p.tab_id == made.tab_id and not p.is_plugin}
                    pids = find_agents(s.name, ids)
                except _Fail:
                    pids = []
                if pids or time.monotonic() >= deadline:
                    break
                nav.sleep(0.2)
        rec = {"worktreePath": path, "session": d.get("session"), "tabName": made.tab_name or d["slug"],
               "tabId": made.tab_id, "agentPids": pids}
        e = jr.begin("link", i, written=rec)

        def persist_prev(prev: dict | None) -> None:
            # under links.lock, BEFORE the swap: the record we replace is durable in the journal
            try:
                jr.update(e, "swapping", previous=prev)
            except _Fail:
                e["state"] = "pending"          # not persisted -> swap_link_journaled does not swap
                raise

        def write() -> None:
            links.swap_link_journaled(rec, persist_prev)
        try:
            run.internal(f"links.json: {d.get('session')} › {rec['tabName']} ({path})", write)
        except (OSError, ValueError, _Fail):
            if e["state"] == "pending":
                jr.update(e, "absent", strict=False)   # failed before the swap: nothing written
            raise
        if not dry:
            jr.update(e, "created")
        return f"{len(pids)} agent(s)" if pids else ""

    def do_switch(i: int, st: dict) -> str:
        if dry:
            return ""
        if d["target"] == "session":
            if made.window is not None:
                nav.focus_window(*made.window)
            return ""
        s = made.zsession
        if s is None or made.tab_id is None:
            return ""
        node = SessionNode(s)
        res = nav.go(Target(node, tab_id=made.tab_id, label=f"{s.name} › {d['slug']}", worktree=path,
                            roots=frozenset({repo, path})))
        if not res.ok:
            notes.append(f"created, but not switched: {res.message}")
            switched[0] = False
        return ""

    def _has_agent() -> bool:
        return any(st.get("layout_string") or st.get("layout") or st.get("kind") == "layout_file"
                   or (st.get("kind") == "session" and "-n" in st.get("argv", [])) for st in spec)

    switched = [True]
    handlers = {"exclude": do_exclude, "worktree": do_worktree, "env": do_env, "install": do_install,
                "tab": do_tab, "layout_file": do_layout_file, "session": do_session, "links": do_links,
                "switch": do_switch, "provider_setup": do_provider_setup}
    state = ["pending"] * total
    failure: str | None = None
    cancelled = False
    jr.save()
    for i, st in enumerate(spec):
        if cancel.is_set():
            cancelled = True
            break
        state[i] = "running"
        ev(i, "running")
        try:
            detail = handlers[st["kind"]](i, st)
        except _Cancelled:
            state[i] = "failed"
            ev(i, "failed", "cancelled")
            cancelled = True
            break
        except (_Fail, OSError) as e:
            state[i] = "failed"
            ev(i, "failed", str(e))
            failure = str(e)
            break
        state[i] = "ok"
        ev(i, "ok", detail)

    if failure is None and not cancelled:
        jr.close()
        if dry:
            n = len(run.acted)
            return ActionResult(True, f"dry-run: {n} command{'s' if n != 1 else ''} logged")
        where = d.get("session")
        msg = f"created {d['slug']} in {where}" + (f" ({'; '.join(notes)})" if notes else "")
        return ActionResult(True, msg, close=bool(d["switch"]) and switched[0], refresh=True)

    for j in range(total):
        if state[j] == "pending":
            ev(j, "skipped")
    why = "cancelled" if cancelled else f"failed: {failure}"
    if dry:
        jr.close()
        return ActionResult(False, f"spawn {why}")
    rolled, kept = rollback(jr, nav, d, ev, links)
    if not kept:
        jr.close()                         # everything accounted for; otherwise leave the trace
    tail = f"; rolled back: {', '.join(rolled)}" if rolled else "; nothing to roll back"
    extra = notes + kept
    if extra:
        tail += f" ({'; '.join(extra)})"
    return ActionResult(False, f"spawn {why}{tail}", refresh=bool(rolled or kept))


def _unlink_if_same(e: dict) -> bool:
    try:
        st = os.lstat(e["path"])
    except FileNotFoundError:
        e["state"] = "removed"
        return True
    if e.get("ino") is not None and st.st_ino != e["ino"]:
        return False
    os.unlink(e["path"])
    e["state"] = "removed"
    return True


def _age_ok(e: dict, fresh_age: int | None) -> bool:
    """The session is the incarnation we saw: its created-age grew by the elapsed time (±5 s)."""
    if e.get("age") is None or fresh_age is None:
        return False
    seen = e.get("seen", e.get("at", time.time()))
    expected = e["age"] + (time.time() - seen)
    return abs(fresh_age - expected) <= 5 + 1


CWD_SETTLE_S = 1.0     # a just-started shell may not report its cwd yet


def _inside(cwd: str | None, path: str) -> bool:
    return bool(cwd) and (cwd == path or cwd.startswith(path.rstrip("/") + "/"))


def _live_panes(nav: Navigator, s: ZSession, tab_id: int | None = None):
    """(tabs, live non-plugin terminal panes [of `tab_id`]), re-listed briefly while a pane has
    not reported its cwd yet."""
    deadline = time.monotonic() + CWD_SETTLE_S
    while True:
        tabs, panes = nav.fresh_inventory(s)
        live = [p for p in panes if not p.is_plugin and not p.exited and (tab_id is None or p.tab_id == tab_id)]
        if all(p.cwd for p in live) or time.monotonic() >= deadline:
            return tabs, panes, live
        nav.sleep(0.1)


def _outside(nav: Navigator, s: ZSession, live, path: str) -> list[str]:
    """Panes not inside the spawned worktree; agentree's own pane is ignored (its tab is refused)."""
    own = int(nav.env.pane_id) if nav.env.pane_id and nav.env.pane_id.isdigit() and \
        nav.env.is_current(s.name, s.socket_dir) else None
    return [f"{p.zid}:{p.cwd or '?'}" for p in live if p.pane_id != own and not _inside(p.cwd, path)]


def _tab_status(nav: Navigator, e: dict) -> tuple[str, str]:
    """("ours" | "gone" | "keep", why). Ours = same session incarnation, the tab exists with the
    name we gave it, ALL its live terminal panes are inside the spawned worktree, and it does not
    hold agentree's own pane."""
    f = nav.lookup_session(e["session"])
    if f is None or f.live_dir is None:
        return "gone", "session gone"
    if canonical_dir(f.live_dir) != canonical_dir(e["socket_dir"]) or not _age_ok(e, f.age_s):
        return "keep", "session restarted or moved"
    s = ZSession(e["session"], "attached", None, f.live_dir, 1)
    tabs, panes, live = _live_panes(nav, s, e["tab_id"])
    tab = next((t for t in tabs if t.tab_id == e["tab_id"]), None)
    if tab is None:
        return "gone", "already gone"
    if tab.name != e["name"]:
        return "keep", f"now named {tab.name!r}"
    if nav.env.own_tab(e["session"], f.live_dir, panes) == e["tab_id"]:
        return "keep", "agentree itself runs in it"
    if not live:
        return "keep", "it has no live panes to identify it by"
    out = _outside(nav, s, live, e["cwd"])
    if out:
        return "keep", f"it also has panes outside the worktree ({', '.join(out)})"
    return "ours", ""


def _session_status(nav: Navigator, e: dict, path: str, pending: bool) -> tuple[str, str, Any]:
    """("ours" | "gone" | "keep", why, fresh). A pending session (launch issued, never observed)
    is ours only if it is live in the expected dir and younger than our launch."""
    f = nav.lookup_session(e["name"])
    if f is None:
        return ("keep", "launched but never seen; check `zellij ls`", None) if pending else ("gone", "", None)
    if f.live_dir is None:
        return "keep", "exited (resurrectable) — delete it yourself if it is ours", f
    if canonical_dir(f.live_dir) != canonical_dir(e["socket_dir"]):
        return "keep", f"lives in {f.live_dir}, expected {e['socket_dir']}", f
    if pending:
        if f.age_s is None or f.age_s > (time.time() - e["at"]) + 5 + 1:
            return "keep", "a session with that name predates our launch", f
    elif not _age_ok(e, f.age_s):
        return "keep", "not the incarnation we created", f
    if nav.env.is_current(e["name"], f.live_dir):
        return "keep", "agentree itself runs in it", f
    s = ZSession(e["name"], "attached", None, f.live_dir, 1)
    _tabs, _panes, live = _live_panes(nav, s)
    out = _outside(nav, s, live, path)
    if out:
        return "keep", f"it has panes outside the worktree ({', '.join(out)})", f
    return "ours", "", f


def rollback(jr: Journal, nav: Navigator, d: dict, ev, links) -> tuple[list[str], list[str]]:
    """Undo in reverse what the journal says this run created, re-validating each resource's
    identity first. Anything uncertain or any failed cleanup stays an unresolved entry (journal
    kept, step "failed" with a kept/uncertain detail). Returns (rolled back, kept notes)."""
    run = nav.runner
    out: list[str] = []
    kept: list[str] = []
    repo, path = d["repo"], d["worktree_path"]

    def keep(e: dict, msg: str) -> None:
        kept.append(msg)
        jr.update(e, strict=False, unresolved=msg)
        ev(e["step"], "failed", msg)

    def done(e: dict, what: str | None, detail: str = "") -> None:
        jr.update(e, "rolled_back" if what else "resolved", strict=False)
        if what:
            out.append(what)
            ev(e["step"], "rolled_back", detail or what)

    def gone_now(name: str) -> bool:
        f = nav.lookup_session(name)
        return f is None

    sessions: dict[str, str] = {}          # name -> "rolled_back" | "gone" | "kept"
    for e in reversed(jr.entries):
        kind, st = e["kind"], e["state"]
        if kind == "window" or st in ("absent", "foreign", "rolled_back", "resolved"):
            continue
        try:
            if kind == "link":
                if st == "pending":
                    keep(e, f"uncertain: links.json record for {path} — check it")
                    continue
                if links.restore_link(e["written"], e.get("previous")):
                    done(e, "links.json")
                    continue
                cur = next((r for r in links.load_links() if r.get("worktreePath") == e["written"]["worktreePath"]),
                           None)
                if cur == e.get("previous"):
                    done(e, None)                       # the swap never landed
                else:
                    keep(e, f"kept: links.json record for {path} changed since; previous one is in the journal")
            elif kind == "tab":
                if st == "pending" or e.get("tab_id") is None:
                    keep(e, f"uncertain: a tab may have been created in {e['session']} (no id reported)")
                    continue
                status, why = _tab_status(nav, e)
                if status == "gone":
                    done(e, None)
                elif status == "keep":
                    keep(e, f"kept: tab {e['tab_id']} in {e['session']}: {why}; close it yourself if it is "
                            f"the spawned {e['name']}")
                else:
                    s = ZSession(e["session"], "attached", None, e["socket_dir"], 1)
                    r = run.act(*nav.zj(s, "close-tab-by-id", str(e["tab_id"])))
                    if r.ok:
                        done(e, f"tab {e['tab_id']}", f"closed tab {e['tab_id']}")
                    else:
                        keep(e, f"kept: could not close tab {e['tab_id']} in {e['session']}: "
                                f"{(r.stderr or '').strip()[:120] or r.code}")
            elif kind == "session":
                status, why, f = _session_status(nav, e, path, pending=st == "pending")
                if status == "gone":
                    sessions[e["name"]] = "gone"
                    done(e, None)
                    continue
                if status == "keep":
                    sessions[e["name"]] = "kept"
                    keep(e, f"{'uncertain' if st == 'pending' else 'kept'}: session {e['name']}: {why}")
                    continue
                env = {"ZELLIJ_SOCKET_DIR": f.live_dir}
                r1 = run.act([nav.zellij, "kill-session", e["name"]], env)
                r2 = run.act([nav.zellij, "delete-session", "--force", e["name"]], env)
                if run.dry_run or gone_now(e["name"]):
                    sessions[e["name"]] = "rolled_back"
                    done(e, f"session {e['name']}", f"killed session {e['name']}")
                else:
                    sessions[e["name"]] = "kept"
                    errs = "; ".join(x for x in ((r1.stderr or "").strip(), (r2.stderr or "").strip()) if x)
                    keep(e, f"kept: could not kill session {e['name']}: {errs[:160] or 'still listed'}")
            elif kind == "layout_file":
                if st == "pending" and not os.path.lexists(e["path"]):
                    done(e, None)
                elif st == "pending" or not _unlink_if_same(e):
                    keep(e, f"kept: {e['path']} (not the file we wrote?)")
                else:
                    done(e, None)
            elif kind == "worktree":
                if st != "created":
                    keep(e, f"uncertain: {path} may have been created; check `git worktree list` in {repo}")
                    continue
                ok, why = pristine(run, path, e.get("head"), e.get("baseline"), _copied(jr))
                if not ok:
                    keep(e, f"kept {path}: {why}")
                    continue
                r = run.act(["git", "-C", repo, "worktree", "remove", "--force", path], timeout=60.0)
                if r.ok:
                    done(e, "worktree", f"removed {path}")
                    for x in jr.entries:
                        if x["kind"] == "env" and x["state"] == "created":
                            jr.update(x, "resolved", strict=False)
                else:
                    keep(e, f"kept {path}: {(r.stderr or '').strip()[:120]}")
            elif kind == "branch":
                ref = f"refs/heads/{e['name']}"
                tip = run.query(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref])
                now = tip.stdout.strip() if tip.ok else None
                if st == "pending":
                    if now is None:
                        done(e, None)
                        continue
                    if now != e.get("sha"):
                        jr.update(e, "foreign", strict=False)
                        continue
                wt = next((x for x in jr.entries if x["kind"] == "worktree"), None)
                if wt is not None and wt["state"] not in ("rolled_back", "resolved", "absent"):
                    keep(e, f"kept: branch {e['name']} (its worktree was kept)")
                    continue
                if now is None:
                    done(e, None)
                    continue
                if now != e["sha"]:
                    keep(e, f"kept: branch {e['name']} moved since creation")
                    continue
                r = run.act(["git", "-C", repo, "update-ref", "-d", ref, e["sha"]])
                if r.ok:
                    done(e, f"branch {e['name']}", f"deleted branch {e['name']}")
                else:
                    keep(e, f"kept: branch {e['name']}: {(r.stderr or '').strip()[:120]}")
        except (_Fail, OSError) as ex:
            keep(e, f"kept: {_describe(e)}: cannot verify it ({ex})")

    # kitty windows last: a window ends with its zellij client, so it depends on the session result
    for e in reversed(jr.entries):
        if e["kind"] != "window" or e["state"] in ("absent", "rolled_back", "resolved"):
            continue
        sess = sessions.get(e["session"])
        if e["state"] == "pending" or e.get("window_id") is None:
            if sess in ("rolled_back", "gone"):
                done(e, None)
            else:
                keep(e, f"uncertain: a kitty window may show session {e['session']}")
            continue
        try:
            st = _window_state(nav, e)
            if st == "gone":
                done(e, None)
            elif sess == "kept":
                keep(e, f"kept: kitty window {e['window_id']} (shows the kept session {e['session']})")
            elif st == "ours":
                deadline = time.monotonic() + 1.0
                while sess == "rolled_back" and st == "ours" and time.monotonic() < deadline:
                    nav.sleep(0.1)              # it usually closes by itself once the client exits
                    st = _window_state(nav, e)
                if st == "gone":
                    done(e, None)
                    continue
                r = run.act([nav.kitty, "@", "--to", e["sock"], "close-window", "--match", f"id:{e['window_id']}"])
                if r.ok:
                    done(e, f"window {e['window_id']}")
                else:
                    keep(e, f"kept: could not close kitty window {e['window_id']}: "
                            f"{(r.stderr or '').strip()[:120] or r.code}")
            else:
                keep(e, f"kept: kitty window {e['window_id']} no longer shows {e['session']}")
        except (_Fail, OSError) as ex:
            keep(e, f"kept: {_describe(e)}: cannot verify it ({ex})")
    jr.save(strict=False)
    return out, kept


def _describe(e: dict) -> str:
    k = e["kind"]
    if k == "tab":
        return f"tab {e.get('tab_id', '?')} in {e.get('session')}"
    if k == "session":
        return f"session {e.get('name')}"
    if k in ("worktree", "layout_file"):
        return f"{k} {e.get('path')}"
    if k == "branch":
        return f"branch {e.get('name')}"
    if k == "window":
        return f"kitty window {e.get('window_id', '?')}"
    return k


def _copied(jr: Journal) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in jr.entries:
        if e["kind"] == "env":
            out.update(e.get("files") or {})
    return out


def _window_state(nav: Navigator, e: dict) -> str:
    """"ours" (still exists, still marked agentree_session=<name>), "gone", or "other". Raises _Fail if
    kitty cannot be asked."""
    for win in nav._windows(nav._ls(e["sock"])):
        if win.get("id") == e.get("window_id"):
            return "ours" if (win.get("user_vars") or {}).get("agentree_session") == e["session"] else "other"
    return "gone"
