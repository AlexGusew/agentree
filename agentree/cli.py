"""agentree command line. Imports are lazy so the TUI path paints its first frame fast.

  agentree                   popup TUI (renders the cached snapshot first, refreshes in the background)
  agentree --json [--cached] print the snapshot as JSON (gathered, or the cache only)
  agentree tree              debug: gather and print the tree as plain text
  agentree spawn REPO BRANCH worktree + tab/session (+ agent), with rollback on failure
  agentree doctor            check the environment (exit 1 on any failure)
  agentree <internal>        hidden commands owned by agent providers (e.g. a status hook); see
                        agentree.providers.registry (ProviderSpec.internal_commands)
"""
from __future__ import annotations

import time

_T0 = time.perf_counter()      # process-level start reference for the startup bench

import os  # noqa: E402
import sys  # noqa: E402

MODES = ("all", "with_tab", "dirty", "hide")


def _parser():
    import argparse

    p = argparse.ArgumentParser(prog="agentree", description="zellij workspace / worktree / agent manager")
    p.add_argument("--dry-run", action="store_true", help="actions print what they would run, change nothing")
    p.add_argument("--demo", action="store_true", help="use built-in sample data instead of live sources")
    p.add_argument("--json", action="store_true", help="print the snapshot as JSON and exit")
    p.add_argument("--cached", action="store_true", help="with --json: print the cached snapshot, gather nothing")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    t = sub.add_parser("tree", help="debug: gather and print the tree as plain text")
    t.add_argument("--text", default="", help="text filter")
    t.add_argument("--mode", choices=MODES, help="worktree mode")
    t.add_argument("--panes", action="store_true", help="show panes")
    t.add_argument("--expand", action="store_true", help="expand every fold")
    t.add_argument("--demo", dest="tree_demo", action="store_true", help="use sample data")
    sp = sub.add_parser("spawn", help="create worktree + tab (or session) + agent")
    sp.add_argument("repo", help="repo name (under repo_roots) or path")
    sp.add_argument("branch", help="branch to create (or reuse if it exists and is checked out nowhere)")
    sp.add_argument("--base", choices=("origin", "head"), default="origin",
                    help="branch from origin's default branch (default) or the current HEAD")
    sp.add_argument("--session", action="store_true", help="new zellij session <repo>-<slug> instead of a tab")
    sp.add_argument("--layout", help="zellij layout for the tab/session")
    sp.add_argument("--prompt", default="", help="first message for the agent (uses the built-in agent layout)")
    from agentree.providers import registry

    sp.add_argument("--agent", choices=registry.ids(), default=None,
                    help="agent started by --prompt / --layout agent (default: config default_agent)")
    sp.add_argument("--no-env", action="store_true", help="do not copy .env* files from the root checkout")
    sp.add_argument("--install", action="store_true", help="run the install command for the lockfile")
    sp.add_argument("--no-switch", action="store_true", help="do not switch to it afterwards")
    sp.add_argument("--dry-run", dest="spawn_dry_run", action="store_true", help="log commands, change nothing")
    sub.add_parser("doctor", help="check the environment: tools, kitty, zellij, agents, config")
    return p


def make_actions(cfg, dry_run: bool = False):
    """The single Actions factory used by the TUI."""
    from agentree.actions.real import RealActions

    return RealActions(cfg, dry_run=dry_run)


# --------------------------------------------------------------------------- single instance
# One popup per zellij session. The claim is an exclusive non-blocking fcntl lock on
# <state>/instance-<key>.lock held for the popup's lifetime (key = sha1 of canonical socket dir +
# session name); the record next to it (instance-<key>.json) says who holds it (pid, process
# start time, session, socket dir, pane). A second `agentree` in the same session (keybind pressed
# again) focuses the holder's pane only after `hand_off` has verified the holder (lock still
# held, pid + start time, process env, live pane, one client) and exits before rendering; if the
# holder vanished meanwhile it takes over as the instance. Startup cost: one lock + one small
# write (< 1 ms).

def _state_dir() -> str:
    from agentree.paths import state_dir

    return state_dir()


def instance_paths(socket_dir: str, session: str) -> tuple[str, str]:
    from agentree.actions.runner import identity_key

    base = os.path.join(_state_dir(), f"instance-{identity_key(socket_dir, session)}")
    return base + ".lock", base + ".json"


class Instance:
    """A held single-instance claim."""

    def __init__(self, fd: int, record_path: str, record: dict) -> None:
        self.fd = fd
        self.record_path = record_path
        self.record = record

    def write(self) -> None:
        import json

        tmp = f"{self.record_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.record, fh)
        os.replace(tmp, self.record_path)

    def complete(self) -> None:
        """Add the process start time (sysctl; ~3 ms of ctypes import, so after the first frame)."""
        try:
            self.record["start"] = proc_start_time(os.getpid())
            self.write()
        except OSError:
            pass

    def release(self) -> None:
        try:
            rec = read_instance(self.record_path)
            if rec is not None and rec.get("pid") == os.getpid():
                os.unlink(self.record_path)
        except OSError:
            pass
        try:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def proc_start_time(pid: int) -> float | None:
    from agentree.actions.runner import proc_start_time as f

    return f(pid)


def read_instance(path: str) -> dict | None:
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("pid"), int) else None


def _try_lock(lock: str) -> int | None:
    """fd holding an exclusive lock on `lock`, or None if another process holds it."""
    import fcntl

    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _become(fd: int, record: str, e) -> Instance:
    from agentree.actions.runner import canonical_dir, default_socket_dir

    inst = Instance(fd, record, {"pid": os.getpid(), "start": None, "session": e.get("ZELLIJ_SESSION_NAME"),
                                 "socket_dir": canonical_dir(default_socket_dir(e)),
                                 "pane_id": e.get("ZELLIJ_PANE_ID")})
    try:
        inst.write()
    except OSError:
        pass
    return inst


def claim_instance(environ=None) -> tuple[Instance | None, dict | None]:
    """(claim, None) if we are the session's instance; (None, holder record or {}) if another
    process holds the lock; (None, None) outside zellij or if the state dir is unusable."""
    from agentree.actions.runner import default_socket_dir

    e = os.environ if environ is None else environ
    session = e.get("ZELLIJ_SESSION_NAME")
    if not session:
        return None, None
    lock, record = instance_paths(default_socket_dir(e), session)
    try:
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        fd = _try_lock(lock)
    except OSError:
        return None, None
    if fd is None:
        return None, read_instance(record) or {}
    return _become(fd, record, e), None


def _default_proc_env(pid: int) -> dict | None:
    from agentree.sources import proc

    ae = proc.proc_args_env(pid)
    return ae[1] if ae else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hand_off(rec: dict, environ=None, *, dry_run: bool = False, run=None, proc_env=None,
             start_time=None, pid_alive=None) -> tuple[str, Instance | None]:
    """Second start in a session whose instance lock was taken: (message, claim).

    The holder is verified before its pane is focused: the lock is still held (if we can take
    it now, the holder is gone and *we* become the instance: claim returned), the record names
    this session/socket dir/pane, the recorded pid is alive with the recorded process start
    time (missing start: the holder is still starting -> refuse), its environment carries the
    same ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID / ZELLIJ_SOCKET_DIR, the pane is listed and not
    exited in a fresh list-panes of that explicit session, and exactly one client is attached.
    Dry-run never focuses.
    """
    from agentree.actions.runner import canonical_dir, default_socket_dir

    e = os.environ if environ is None else environ
    session, sdir = e.get("ZELLIJ_SESSION_NAME"), default_socket_dir(e)
    lock, record = instance_paths(sdir, session)
    try:
        fd = _try_lock(lock)
    except OSError:
        fd = None
    if fd is not None:                      # holder exited after our failed claim
        return "previous agentree is gone; taking over", _become(fd, record, e)
    rec = read_instance(record) or rec      # the holder may have completed its record meanwhile
    who = f"pid {rec.get('pid')}" if rec.get("pid") else "another process"
    busy = f"agentree is already running in this session ({who})"
    if dry_run:
        return f"{busy}; dry-run: not focusing it", None
    pane, pid, start = rec.get("pane_id"), rec.get("pid"), rec.get("start")
    if rec.get("session") != session or rec.get("socket_dir") != canonical_dir(sdir) \
            or not (isinstance(pane, str) and pane.isdigit()) or not isinstance(pid, int):
        return busy, None
    if not isinstance(start, (int, float)):
        return f"{busy}; it is still starting — not focusing", None
    if not (pid_alive or _alive)(pid):
        return f"{busy}; but pid {pid} is gone — not focusing", None
    actual = (start_time or proc_start_time)(pid)
    if actual is None or abs(actual - start) > 0.001:
        return f"{busy}; pid {pid} is a different process now — not focusing", None
    penv = (proc_env or _default_proc_env)(pid)
    if not penv or penv.get("ZELLIJ_SESSION_NAME") != session or penv.get("ZELLIJ_PANE_ID") != pane \
            or canonical_dir(default_socket_dir(penv)) != canonical_dir(sdir):
        return f"{busy}; its environment does not match pane terminal_{pane} — not focusing", None
    import json

    from agentree.actions import exec as ex

    run = run or ex.run
    zj = ex.which("zellij") or "zellij"
    env = {"ZELLIJ_SOCKET_DIR": sdir}
    base = [zj, "--session", session, "action"]
    r = run([*base, "list-panes", "--state", "--json"], env=env, timeout=2.0)
    try:
        panes = json.loads(r.stdout or "[]") if r.ok else []
    except ValueError:
        panes = []
    hit = next((p for p in panes if not p.get("is_plugin") and str(p.get("id")) == pane), None)
    if hit is None or hit.get("exited"):
        return f"{busy} but its pane terminal_{pane} is {'exited' if hit else 'not'} in {session}", None
    r = run([*base, "list-clients"], env=env, timeout=2.0)
    n = sum(1 for ln in (r.stdout or "").splitlines() if ln.split() and ln.split()[0].isdigit()) if r.ok else -1
    if n != 1:
        return f"{busy} in pane terminal_{pane}; {max(n, 0)} clients — not focusing", None
    run([*base, "go-to-tab-by-id", str(hit.get("tab_id"))], env=env, timeout=2.0, log=True)
    run([*base, "focus-pane-id", f"terminal_{pane}"], env=env, timeout=2.0, log=True)
    return f"focused the running agentree (pane terminal_{pane})", None


def _demo_snapshot():
    from agentree.model.sample import sample_snapshot

    return sample_snapshot(time.time())


def _restamp(snap):
    import dataclasses

    return dataclasses.replace(snap, taken_at=time.time(), stale=False)


def _bench_hook():
    path = os.environ.get("AGENTREE_BENCH_FIRST_FRAME")
    if not path:
        return None

    def hook() -> None:
        ms = (time.perf_counter() - _T0) * 1000
        with open(path, "w") as fh:
            fh.write(f"{time.time():.6f} {ms:.3f}\n")
        raise SystemExit(0)

    return hook


def cmd_tui(*, demo: bool = False, dry_run: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("agentree: the TUI needs a terminal (stdin and stdout must be a tty).\n"
              "     try `agentree --json` or `agentree tree` for non-interactive output.", file=sys.stderr)
        return 1
    inst, running = (None, None) if demo else claim_instance()
    if running is not None:
        msg, inst = hand_off(running, dry_run=dry_run)
        if inst is None:
            print(f"agentree: {msg}", file=sys.stderr)
            return 0
    try:
        return _tui(demo=demo, dry_run=dry_run, inst=inst)
    finally:
        if inst is not None:
            inst.release()


def _tui(*, demo: bool, dry_run: bool, inst: Instance | None = None) -> int:
    from agentree import state
    from agentree.config import load_config

    cfg = load_config()
    live = None
    if demo:
        snapshot = _demo_snapshot()
        refresh = lambda: _restamp(snapshot)  # noqa: E731
        save_ui = lambda ui: None  # noqa: E731 - demo never persists UI state
    else:
        snapshot = state.load_cache()
        if snapshot is None:
            from agentree.types import Snapshot

            snapshot = Snapshot(taken_at=time.time(), stale=True)
        if os.environ.get("AGENTREE_LIVE", "1") != "0":
            # live agent status: provider event feeds + agents-only refreshes (imported lazily)
            from agentree.gather import LiveStatus

            live = LiveStatus(cfg)
            refresh = live.refresh
        else:
            def refresh():
                from agentree.gather import gather

                return gather(cfg)

        save_ui = state.save_ui
    ui = state.load_ui(cfg.default_ui())
    if demo:
        from agentree.actions.api import NullActions

        actions = NullActions(dry_run=dry_run)     # demo data must never drive real commands
    else:
        actions = make_actions(cfg, dry_run)
    from agentree.ui.app import run_app

    first = _bench_hook() or (inst.complete if inst is not None else None)
    return run_app(cfg=cfg, snapshot=snapshot, ui=ui, refresh=refresh, actions=actions,
                   save_ui=save_ui, on_first_frame=first, live=live)


def cmd_json(*, cached: bool, demo: bool) -> int:
    import json

    from agentree.model.snapshot import to_dict

    if demo:
        snap = _demo_snapshot()
    elif cached:
        from agentree import state

        snap = state.load_cache()
        if snap is None:
            print("agentree: no cached snapshot (run `agentree --json` once)", file=sys.stderr)
            return 1
    else:
        from agentree.config import load_config
        from agentree.gather import gather

        snap = gather(load_config())
    sys.stdout.write(json.dumps(to_dict(snap), indent=2) + "\n")
    return 0


def _expand_all(snap, ui, now, home):
    import dataclasses

    from agentree.model.tree import build_view

    for _ in range(8):
        view = build_view(snap, ui, now, home=home)
        closed = [r.key for r in view.rows if r.caret == "closed"]
        if not closed:
            break
        folds = dict(ui.folds)
        folds.update((k, True) for k in closed)
        ui = dataclasses.replace(ui, folds=tuple(folds.items()))
    return ui


def cmd_tree(args) -> int:
    import dataclasses

    from agentree.config import load_config
    from agentree.model.tree import build_view, render_plain

    cfg = load_config()
    demo = args.demo or args.tree_demo
    if demo:
        from agentree.model.sample import HOME

        snap, home = _demo_snapshot(), HOME
    else:
        from agentree.gather import gather

        snap, home = gather(cfg), os.path.expanduser("~")
    ui = cfg.default_ui()
    kw: dict = {"text": args.text}
    if args.mode:
        kw["worktree_mode"] = args.mode
    if args.panes:
        kw["show_panes"] = True
    ui = dataclasses.replace(ui, **kw)
    now = snap.taken_at
    if args.expand:
        ui = _expand_all(snap, ui, now, home)
    print(render_plain(build_view(snap, ui, now, home=home)))
    return 0


def resolve_repo(arg: str, repos) -> str | None:
    if os.path.isdir(os.path.join(os.path.expanduser(arg), ".git")):
        return os.path.realpath(os.path.expanduser(arg))
    hits = [r for r in repos if os.path.basename(r) == arg]
    return hits[0] if len(hits) == 1 else None


def spawn_agent(args, cfg) -> str:
    """--agent (a registered provider id; argparse enforces it), else registry.default_id(cfg)
    (config default_agent if registered, else the compat default)."""
    from agentree.providers import registry

    a = getattr(args, "agent", None)
    return a if a in registry.ids() else registry.default_id(cfg)


def cmd_spawn(args, *, dry_run: bool, actions=None, snapshot=None, out=None) -> int:
    """Plan, print, execute with progress lines; Ctrl-C cancels (and rolls back)."""
    import threading

    from agentree.actions.api import SpawnRequest
    from agentree.config import load_config

    out = out or sys.stdout
    cfg = load_config()
    if snapshot is None:
        from agentree.gather import gather

        snapshot = gather(cfg)
    from agentree.sources import git as gsrc

    repo = resolve_repo(args.repo, gsrc.discover_repos(list(cfg.expanded_repo_roots())))
    if repo is None:
        print(f"agentree spawn: repo {args.repo!r} not found (name under repo_roots, or a path)", file=sys.stderr)
        return 2
    actions = actions or make_actions(cfg, dry_run)
    req = SpawnRequest(repo=repo, branch=args.branch, base=args.base,
                       target="session" if args.session else "tab", layout=args.layout, prompt=args.prompt,
                       copy_env=not args.no_env, run_install=args.install, switch_after=not args.no_switch,
                       agent=spawn_agent(args, cfg))
    plan = actions.plan_spawn(req, snapshot)
    import shlex

    print("plan:", file=out)
    for st in plan.steps:
        print("  $ " + (shlex.join(st) if len(st) > 1 else st[0]), file=out)
    for w in plan.warnings:
        print(f"  ! {w}", file=out)
    for e in plan.errors:
        print(f"  ✗ {e}", file=out)
    if plan.errors:
        return 1
    cancel = threading.Event()
    result: list = []
    last: dict[int, str] = {}

    def progress(ev) -> None:
        if ev.state == "running" and ev.detail:
            print(f"      | {ev.detail}", file=out, flush=True)
            return
        if last.get(ev.index) == ev.state:
            return
        last[ev.index] = ev.state
        mark = {"running": "…", "ok": "✓", "failed": "✗", "skipped": "-", "rolled_back": "↺"}.get(ev.state, "?")
        detail = f" — {ev.detail}" if ev.detail else ""
        print(f"[{ev.index + 1}/{ev.total}] {mark} {ev.label}{detail}", file=out, flush=True)

    t = threading.Thread(target=lambda: result.append(actions.execute(plan, None, progress=progress,
                                                                      cancel=cancel)), daemon=True)
    t.start()
    while t.is_alive():
        try:
            t.join(0.2)
        except KeyboardInterrupt:
            print("cancelling — rolling back…", file=out, flush=True)
            cancel.set()
    res = result[0] if result else None
    if res is None:
        print("agentree spawn: no result", file=sys.stderr)
        return 1
    print(("✓ " if res.ok else "✗ ") + (res.message or ("done" if res.ok else "failed")), file=out)
    return 0 if res.ok else 1


def cmd_doctor(out=None) -> int:
    from agentree.config import load_config
    from agentree.doctor import run_doctor

    return run_doctor(load_config(), out=out or sys.stdout)


def _kill_children() -> None:
    """Kill every subprocess still registered in exec (e.g. a refresh abandoned on quit).
    exec is only imported if something already used it, so the fast path stays cheap."""
    ex = sys.modules.get("agentree.actions.exec")
    if ex is not None:
        ex.shutdown()


def main(argv: list[str] | None = None) -> int:
    """Process entry point: dispatch, then kill leftover children (also at interpreter exit)."""
    if sys.version_info < (3, 14):
        sys.exit(f"agentree needs Python 3.14+ (this is {sys.version.split()[0]} at {sys.executable})")
    import atexit

    atexit.register(_kill_children)
    try:
        return run(sys.argv[1:] if argv is None else argv)
    finally:
        _kill_children()


def run_internal(argv: list[str]) -> int | None:
    """Dispatch a provider-owned internal command (registry.internal_owner(argv[0]) ->
    provider.run_internal); None if argv[0] is not one. Only the registry (descriptors) and the
    owning provider module are imported: no gather / UI on this path (hook latency)."""
    from agentree.providers import registry

    owner = registry.internal_owner(argv[0])
    if owner is None:
        return None
    return registry.get(owner.id).run_internal(argv[0], argv[1:])


def run(argv: list[str]) -> int:
    """Dispatch a command line (no process-exit side effects; used by tests)."""
    if argv and not argv[0].startswith("-"):
        rc = run_internal(argv)                 # provider-owned hidden commands (hooks): tiny
        if rc is not None:
            return rc
    # Fast path for the popup: no argparse import when only TUI flags are given.
    if set(argv) <= {"--dry-run", "--demo"}:
        return cmd_tui(demo="--demo" in argv, dry_run="--dry-run" in argv)
    args = _parser().parse_args(argv)
    if args.cmd == "spawn":
        return cmd_spawn(args, dry_run=args.dry_run or args.spawn_dry_run)
    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "tree":
        return cmd_tree(args)
    if args.cached and not args.json:
        print("agentree: --cached only applies to --json", file=sys.stderr)
        return 2
    if args.json:
        return cmd_json(cached=args.cached, demo=args.demo)
    return cmd_tui(demo=args.demo, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
