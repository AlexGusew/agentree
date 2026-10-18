#!/usr/bin/env python3
"""Live verification (spawn / remove / rename / kill) on disposable resources only.

Per run (random id): a scratch repo `agentreetest-<id>-repo` (+ a local bare `origin`, so origin/HEAD
exists; no network) inside a fresh temp dir, a session `agentreetest-<id>-a` attached in a marked kitty
tab, an inert opencode substitute (records its argv, sleeps) as `opencode_cmd`, a temp
AGENTREE_STATE_DIR and AGENTREE_CONFIG. Sessions agentree creates for the run are named
`agentreetest-<id>-repo-<slug>` and registered in the Ownership record only after they were verified
not to exist before. Cleanup touches only what the Ownership record lists and kitty windows that
are marked for this run and still corroborated as one of its sessions. Kitty focus is restored.

Run from a plain kitty window (not inside zellij):  python3 scripts/live_spawn.py [--keep]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentree.actions import exec as ex  # noqa: E402
from agentree.sources import kitty as ksrc  # noqa: E402

RUN = secrets.token_hex(3)
PREFIX = f"agentreetest-{RUN}-"
A = PREFIX + "a"
REPO_NAME = PREFIX + "repo"
UID = os.getuid()
DIR = f"/tmp/zellij-{UID}"
ZJ = ex.which("zellij") or "zellij"
KT = ksrc.kitty_bin()
GIT = ["git", "-c", "user.name=agentreetest", "-c", "user.email=agentreetest@example.invalid"]
PROMPT = 'it\'s "quoted"\nsecond line \\ with backslash'

RESULTS: list[tuple[str, bool, str]] = []
SOCK = ""
LOCKED = False


class Abort(Exception):
    pass


def check(name: str, ok, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return bool(ok)


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZELLIJ")}
    env["ZELLIJ_SOCKET_DIR"] = DIR
    env.update(extra)
    return env


def sh(argv, *, env=None, cwd=None, check_rc=False) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, env=env or clean_env(), cwd=cwd, capture_output=True, text=True, timeout=30)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"{argv}: {r.returncode} {r.stderr.strip()}")
    return r


class Ownership:
    def __init__(self) -> None:
        self.sessions: list[str] = []

    def add(self, name: str) -> None:
        if not name.startswith(PREFIX):
            raise Abort(f"refusing to own {name!r}: not this run's name")
        if name not in self.sessions:
            self.sessions.append(name)

    def rename(self, old: str, new: str) -> None:
        if old in self.sessions:
            self.sessions[self.sessions.index(old)] = new


OWNED = Ownership()
SUBSTITUTES: list[tuple[int, float]] = []      # (pid, start time) of agent stand-ins we started


def track_substitutes(sleeper: str) -> None:
    """Record the stand-in processes (exact executable path) by pid + start time."""
    from agentree.actions.runner import proc_start_time

    out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    for ln in out.splitlines():
        pid_s, _, cmd = ln.strip().partition(" ")
        if cmd.split(" ", 1)[0] == sleeper and pid_s.isdigit():
            st = proc_start_time(int(pid_s))
            if st is not None and (int(pid_s), st) not in SUBSTITUTES:
                SUBSTITUTES.append((int(pid_s), st))


def stop_substitutes() -> None:
    from agentree.actions.runner import proc_start_time

    for pid, st in SUBSTITUTES:
        now = proc_start_time(pid)
        if now is not None and abs(now - st) < 0.001:     # still the very process we started
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def ours(name: str) -> str:
    if name not in OWNED.sessions:
        raise Abort(f"refusing to act on session {name!r}: not created by this run")
    return name


def zj(session: str, *args: str) -> subprocess.CompletedProcess:
    return sh([ZJ, "--session", ours(session), "action", *args])


def tabs(session: str) -> list[dict]:
    r = zj(session, "list-tabs", "--all", "--json")
    return json.loads(r.stdout or "[]") if r.returncode == 0 else []


def panes(session: str) -> list[dict]:
    r = zj(session, "list-panes", "--tab", "--command", "--state", "--json")
    return [p for p in json.loads(r.stdout or "[]") if not p.get("is_plugin")] if r.returncode == 0 else []


def clients(session: str) -> int:
    r = zj(session, "list-clients")
    return sum(1 for ln in r.stdout.splitlines() if ln.split() and ln.split()[0].isdigit())


def session_names() -> dict[str, bool]:
    """{name: live} across both socket dirs."""
    out: dict[str, bool] = {}
    from agentree.sources import zellij as zsrc
    for d in {DIR, os.path.join(os.path.realpath(os.environ.get("TMPDIR") or "/tmp"), f"zellij-{UID}")}:
        for ps in zsrc.parse_list_sessions(sh([ZJ, "list-sessions", "-n"], env=clean_env(ZELLIJ_SOCKET_DIR=d)).stdout):
            out[ps.name] = out.get(ps.name, False) or not ps.exited
    return out


def kitty(*args: str) -> subprocess.CompletedProcess:
    return sh([KT, "@", "--to", SOCK, *args], env=os.environ.copy())


def windows() -> list[tuple[dict, dict, dict]]:
    data = json.loads(kitty("ls").stdout or "[]")
    return [(ow, t, w) for ow in data for t in ow.get("tabs") or [] for w in t.get("windows") or []]


def screen_locked() -> bool:
    import plistlib
    try:
        root = plistlib.loads(subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True,
                                             timeout=5).stdout)
    except Exception:  # noqa: BLE001
        return False
    return any(u.get("CGSSessionScreenIsLocked") for u in root.get("IOConsoleUsers") or [])


def focused_window() -> int | None:
    key = "is_active" if LOCKED else "is_focused"
    for ow, t, w in windows():
        if ow.get(key) and t.get(key) and w.get(key):
            return w["id"]
    return None


def our_windows() -> list[int]:
    names = set(OWNED.sessions)
    out = []
    for _ow, _t, w in windows():
        uv = w.get("user_vars") or {}
        marked = uv.get("agentreetest_run") == RUN or uv.get("agentree_session") in names
        if marked and ksrc.resolve_window(w, names) in names:
            out.append(w["id"])
    return out


def wait(pred, timeout: float = 5.0, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() >= deadline:
            return v
        time.sleep(step)


def git_ok(repo: str, *args: str) -> bool:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True).returncode == 0


# --------------------------------------------------------------------------- setup

def make_repo(work: str) -> str:
    repo = os.path.join(work, REPO_NAME)
    if os.path.exists(repo):
        raise Abort(f"{repo} already exists")
    os.makedirs(repo)
    sh([*GIT, "init", "-q", "-b", "main"], cwd=repo, check_rc=True)
    files = {"README": "scratch\n", ".gitignore": ".env\nnode_modules/\n", "package-lock.json": "{}\n", ".env": "SECRET=1\n"}
    for n, body in files.items():
        with open(os.path.join(repo, n), "w") as fh:
            fh.write(body)
    sh([*GIT, "add", "README", ".gitignore", "package-lock.json"], cwd=repo, check_rc=True)
    sh([*GIT, "commit", "-q", "-m", "init"], cwd=repo, check_rc=True)
    origin = os.path.join(work, "origin.git")
    sh([*GIT, "init", "-q", "--bare", origin], check_rc=True)
    sh([*GIT, "remote", "add", "origin", origin], cwd=repo, check_rc=True)
    sh([*GIT, "push", "-q", "origin", "main"], cwd=repo, check_rc=True)        # local bare repo
    sh([*GIT, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=repo, check_rc=True)
    return os.path.realpath(repo)


def make_agent_substitute(work: str) -> tuple[str, str]:
    """`bin/agent` (opencode_cmd): records its argv to agent-argv.json, then execs a compiled
    sleeper named `.opencode-agentreetest` (counts as opencode for agentree's process scan). Notes from this
    machine: a binary named exactly `opencode` outside the nix store is SIGKILLed at exec, and
    Apple platform binaries (/bin/sleep) hide their environment from KERN_PROCARGS2 — so neither
    can stand in for an agent whose ZELLIJ_PANE_ID agentree must read."""
    bindir = os.path.join(work, "bin")
    os.makedirs(bindir)
    src = os.path.join(work, "sleeper.c")
    with open(src, "w") as fh:
        fh.write("#include <unistd.h>\nint main(void){sleep(600);return 0;}\n")
    sleeper = os.path.join(bindir, ".opencode-agentreetest")
    sh(["cc", "-o", sleeper, src], check_rc=True)
    rec = os.path.join(work, "agent-argv.json")
    path = os.path.join(bindir, "agent")
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n"
                 f"{sys.executable} -c 'import json,sys; json.dump(sys.argv[2:], open(sys.argv[1], \"w\"))' "
                 f"{rec} \"$@\"\n"
                 f'exec {sleeper} "$@"\n')
    os.chmod(path, 0o755)
    return path, rec


def create_session(name: str, layout_path: str) -> None:
    if name in session_names():
        raise Abort(f"session {name} already exists — not ours, refusing")
    r = sh([ZJ, "attach", "--create-background", name, "options", "--default-layout", layout_path])
    if r.returncode == 0 or name in session_names():
        OWNED.add(name)
    if r.returncode != 0:
        raise RuntimeError(f"creating {name}: {r.stderr.strip()[:200]}")
    if not wait(lambda: len(tabs(name)) > 0, 5):
        raise RuntimeError(f"{name} did not come up")


# --------------------------------------------------------------------------- main

def main() -> int:
    global SOCK, LOCKED
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    if os.environ.get("ZELLIJ_SESSION_NAME"):
        raise SystemExit("run this from a plain kitty window, not inside zellij")
    socks = ksrc.sockets()
    lo = os.environ.get("KITTY_LISTEN_ON")
    SOCK = lo if lo in socks else (socks[0] if len(socks) == 1 else "")
    if not SOCK:
        raise SystemExit(f"need exactly one owned kitty socket, found {socks}")
    LOCKED = screen_locked()
    work = os.path.realpath(tempfile.mkdtemp(prefix=f"agentreetest-{RUN}-"))
    state = os.path.join(work, "state")
    os.environ["AGENTREE_STATE_DIR"] = state
    os.environ["AGENTREE_CONFIG"] = os.path.join(work, "config.toml")
    start = focused_window()
    print(f"run {RUN}: work {work}; kitty {SOCK}; start window {start}" + ("; screen LOCKED" if LOCKED else ""),
          flush=True)
    try:
        repo = make_repo(work)
        oc, argv_file = make_agent_substitute(work)
        with open(os.environ["AGENTREE_CONFIG"], "w") as fh:
            fh.write(f'repo_roots = ["{repo}"]\nzellij_socket_dirs = ["{DIR}"]\nopencode_cmd = "{oc}"\n')
        lay = os.path.join(work, "a.kdl")
        with open(lay, "w") as fh:
            fh.write(f'layout {{\n  tab name="main" cwd="{repo}" {{\n    pane\n  }}\n}}\n')
        create_session(A, lay)
        r = kitty("launch", "--type=tab", "--tab-title", A, "--var", f"agentreetest_run={RUN}", "--var", f"agentree_session={A}",
                  "--env", f"ZELLIJ_SOCKET_DIR={DIR}", ZJ, "attach", A)
        wid_a = int(r.stdout.strip())
        check("setup: kitty tab attached to session a", wait(lambda: clients(A) == 1, 5), f"window {wid_a}")
        zj(A, "new-tab", "--name", "tmp")                    # make zellij set the window title
        zj(A, "close-tab")
        wait(lambda: any(w["id"] == wid_a and w["title"].startswith(f"{A} | ") for _o, _t, w in windows()), 5)
        kitty("focus-window", "--match", f"id:{start}")

        from agentree import state as st
        from agentree.actions.api import SpawnRequest
        from agentree.actions.real import RealActions
        from agentree.config import load_config
        from agentree.gather import gather
        from agentree.types import Row

        cfg = load_config()
        if cfg.warnings or cfg.opencode_cmd != oc:
            raise Abort(f"temp config not loaded: {cfg.warnings}")
        environ = {"ZELLIJ_SOCKET_DIR": DIR, "KITTY_LISTEN_ON": SOCK}

        def actions(c=cfg) -> RealActions:
            return RealActions(c, environ=environ)

        def snapshot():
            s = gather(cfg)
            names = set(OWNED.sessions)
            return dataclasses.replace(s, sessions=tuple(n for n in s.sessions if n.session.name in names))

        def wt_path(slug: str) -> str:
            return os.path.join(repo, ".worktrees", slug)

        def links() -> list[dict]:
            return st.load_links()

        def tab_named(session: str, name: str) -> dict | None:
            return next((t for t in tabs(session) if t["name"] == name), None)

        def spawn(req: SpawnRequest, c=cfg):
            snap = snapshot()
            plan = actions(c).plan_spawn(req, snap)
            events: list = []
            if plan.errors:
                return plan, None, events
            res = actions(c).execute(plan, None, progress=events.append)
            return plan, res, events

        def row_for_worktree(snap, path):
            for n in snap.sessions:
                for wn in n.worktrees:
                    if wn.worktree.path == path:
                        return Row(key=f"w:{n.session.name}:{path}", kind="worktree", depth=1, caret=None, icon=None,
                                   label=(), meta=(), ref=wn, session=n.session.name)
            for w in snap.orphans.worktrees:
                if w.path == path:
                    from agentree.types import WorktreeNode
                    return Row(key=f"ow:{path}", kind="worktree", depth=1, caret=None, icon=None, label=(), meta=(),
                               ref=WorktreeNode(w), session=None)
            return None

        def session_row(snap, name):
            n = next((n for n in snap.sessions if n.session.name == name), None)
            return n and Row(key=f"s:{name}", kind="session", depth=0, caret=None, icon=None, label=(), meta=(),
                             ref=n, session=name)

        # ---- 1. spawn: new tab in session a
        base = lambda **kw: SpawnRequest(**{**dict(repo=repo, branch="agent/feat-a", base="origin", target="tab",  # noqa: E731
                                                   layout=None, prompt="", copy_env=True, run_install=False,
                                                   switch_after=True), **kw})
        plan, res, events = spawn(base())
        p1 = wt_path("feat-a")
        check("spawn tab: plan ok", not plan.errors, "; ".join(plan.errors) or f"{len(plan.steps)} steps")
        check("spawn tab: result ok + close + refresh", res and res.ok and res.close and res.refresh,
              res.message if res else "")
        check("spawn tab: worktree + branch + .env", os.path.isdir(p1) and git_ok(repo, "show-ref", "--verify", "--quiet",
                                                                                  "refs/heads/agent/feat-a")
              and os.path.isfile(os.path.join(p1, ".env")))
        t1 = tab_named(A, "feat-a")
        cwd_ok = bool(t1) and any(p["tab_id"] == t1["tab_id"] and os.path.realpath(p.get("pane_cwd") or "/") == p1
                                  for p in panes(A))
        check("spawn tab: tab feat-a in session a with cwd = worktree", cwd_ok, f"tab={t1 and t1['tab_id']}")
        check("spawn tab: switched (tab active, kitty window focused)",
              bool(t1) and t1.get("active") and focused_window() == wid_a, f"focused={focused_window()}")
        rec = next((r for r in links() if r.get("worktreePath") == p1), None)
        check("spawn tab: links.json record", rec and rec.get("session") == A and rec.get("tabName") == "feat-a", f"{rec}")
        kitty("focus-window", "--match", f"id:{start}")

        # ---- 2. spawn: new session with a prompt (inert opencode substitute)
        s2 = f"{REPO_NAME}-feat-s"
        if s2 in session_names():
            raise Abort(f"{s2} already exists")
        plan, res, events = spawn(base(branch="agent/feat-s", target="session", prompt=PROMPT))
        if s2 in session_names():
            OWNED.add(s2)                                   # created by agentree in this run
        p2 = wt_path("feat-s")
        check("spawn session: result ok", res and res.ok, res.message if res else "; ".join(plan.errors))
        check("spawn session: session live with one client", session_names().get(s2) and clients(s2) == 1)
        got = wait(lambda: os.path.exists(argv_file) and json.load(open(argv_file)), 5)
        check("spawn session: prompt arrived intact (quotes, newline, backslash)", got == ["--prompt", PROMPT], f"{got!r}")
        track_substitutes(os.path.join(work, "bin", ".opencode-agentreetest"))
        lfs = os.listdir(os.path.join(state, "layouts")) if os.path.isdir(os.path.join(state, "layouts")) else []
        check("spawn session: prompt layout file removed after start", lfs == [], f"{lfs}")
        sp = panes(s2) if s2 in OWNED.sessions else []
        check("spawn session: panes in the worktree",
              bool(sp) and all(os.path.realpath(p.get("pane_cwd") or p2) == p2 for p in sp))
        w2 = [w["id"] for _o, _t, w in windows() if (w.get("user_vars") or {}).get("agentree_session") == s2]
        check("spawn session: its kitty window focused", bool(w2) and focused_window() == w2[0], f"windows={w2}")
        rec = next((r for r in links() if r.get("worktreePath") == p2), None)
        check("spawn session: links.json record", rec and rec.get("session") == s2, f"{rec}")
        kitty("focus-window", "--match", f"id:{start}")

        check("spawn: no journal left behind", st.list_journals() == [], f"{st.list_journals()}")

        # ---- 3a. failure at install -> worktree removed, branch deleted
        bad = dataclasses.replace(cfg, install_commands={"package-lock.json": "sh -c 'echo installing; echo boom >&2; exit 3'"})
        plan, res, events = spawn(base(branch="agent/feat-fail", run_install=True, switch_after=False), c=bad)
        pf = wt_path("feat-fail")
        check("rollback (install fails): failed with rollback", res and not res.ok and "rolled back" in res.message,
              res.message if res else "; ".join(plan.errors))
        check("rollback (install fails): worktree removed, branch deleted",
              not os.path.exists(pf) and not git_ok(repo, "show-ref", "--verify", "--quiet", "refs/heads/agent/feat-fail"))
        check("rollback (install fails): progress shows failed + rolled_back",
              {"failed", "rolled_back"} <= {e.state for e in events})
        # ---- 3b. failure after the tab was created (links.json unwritable) -> tab closed too
        lp = os.path.join(state, "links.json")
        os.replace(lp, lp + ".bak")
        os.makedirs(lp)                                     # a directory: the atomic write fails
        try:
            plan, res, events = spawn(base(branch="agent/feat-late", switch_after=False))
        finally:
            os.rmdir(lp)
            os.replace(lp + ".bak", lp)
        pl = wt_path("feat-late")
        check("rollback (late failure): failed, tab rolled back", res and not res.ok and "tab" in res.message,
              res.message if res else "; ".join(plan.errors))
        check("rollback (late failure): tab closed, worktree removed, branch deleted",
              tab_named(A, "feat-late") is None and not os.path.exists(pl)
              and not git_ok(repo, "show-ref", "--verify", "--quiet", "refs/heads/agent/feat-late"))

        # ---- 4. remove a clean worktree (feat-a): closes its tab, removes worktree + merged branch
        snap = snapshot()
        r4 = row_for_worktree(snap, p1)
        plan = actions().plan_remove(r4, snap) if r4 else None
        steps = [" ".join(s) for s in plan.steps] if plan is not None and hasattr(plan, "steps") else []
        check("remove clean: plan closes tab, removes worktree, deletes merged branch",
              plan is not None and getattr(plan, "confirm", None) is not None and plan.confirm.require_text is None
              and any("close-tab-by-id" in s for s in steps)
              and any("update-ref -d refs/heads/agent/feat-a" in s for s in steps),
              "; ".join(steps)[:300])
        res = actions().execute(plan, None) if hasattr(plan, "steps") else None
        check("remove clean: executed", res and res.ok, res.message if res else "")
        check("remove clean: tab gone, worktree gone, branch gone, link dropped",
              tab_named(A, "feat-a") is None and not os.path.exists(p1)
              and not git_ok(repo, "show-ref", "--verify", "--quiet", "refs/heads/agent/feat-a")
              and not any(r.get("worktreePath") == p1 for r in links()))

        # ---- 5. remove a dirty worktree: requires the typed branch name
        plan, res, _ = spawn(base(branch="agent/feat-d", switch_after=False))
        pd = wt_path("feat-d")
        with open(os.path.join(pd, "README"), "a") as fh:
            fh.write("uncommitted\n")
        snap = snapshot()
        r5 = row_for_worktree(snap, pd)
        plan = actions().plan_remove(r5, snap) if r5 else None
        req_text = getattr(getattr(plan, "confirm", None), "require_text", None)
        check("remove dirty: confirm requires the branch name", req_text == "agent/feat-d", f"require_text={req_text!r}")
        res = actions().execute(plan, "feat-d") if req_text else None
        check("remove dirty: wrong text refused, worktree kept", res and not res.ok and os.path.isdir(pd),
              res.message if res else "")
        res = actions().execute(plan, "agent/feat-d") if req_text else None
        check("remove dirty: typed branch removes it (--force)", res and res.ok and not os.path.exists(pd),
              res.message if res else "")

        # ---- 6. kill the agent substitute in the spawned session
        snap = snapshot()
        agents = [(ag, n.session.name) for n in snap.sessions for wn in n.worktrees for ag in wn.agents] + \
                 [(ag, n.session.name) for n in snap.sessions for ag in n.loose_agents]
        mine = [(ag, sn) for ag, sn in agents if sn == s2]
        check("kill agent: substitute found in the snapshot", len(mine) == 1, f"{[(ag.pid, sn) for ag, sn in agents]}")
        if mine:
            ag, sn = mine[0]
            r6 = Row(key=f"a:{ag.pid}", kind="agent", depth=2, caret=None, icon=None, label=(), meta=(), ref=ag, session=sn)
            plan = actions().plan_remove(r6, snap)
            res = actions().execute(plan, None) if hasattr(plan, "steps") else plan
            gone = wait(lambda: subprocess.run(["kill", "-0", str(ag.pid)], capture_output=True).returncode != 0, 3)
            check("kill agent: SIGTERM after identity check, process gone", res and res.ok and gone,
                  getattr(res, "message", ""))

        # ---- 7. rename the spawned session
        new = PREFIX + "renamed"
        if new in session_names():
            raise Abort(f"{new} already exists")
        snap = snapshot()
        res = actions().rename_session(session_row(snap, s2), snap, new)
        names = session_names()
        if names.get(new):
            OWNED.rename(s2, new)
        check("rename: ok", res and res.ok and res.refresh, getattr(res, "message", ""))
        check("rename: new name live, old name gone", names.get(new) and s2 not in names)
        check("rename: links.json follows", any(r.get("session") == new and r.get("worktreePath") == p2 for r in links()))

        # ---- 8. kill the renamed session
        snap = snapshot()
        plan = actions().plan_remove(session_row(snap, new), snap)
        ok_plan = getattr(plan, "kind", "") == "kill-session" and plan.confirm.danger
        res = actions().execute(plan, None) if ok_plan else None
        check("kill session: confirmed (danger) and executed", ok_plan and res and res.ok, getattr(res, "message", ""))
        check("kill session: no longer live", wait(lambda: not session_names().get(new), 5))
    except Abort as e:
        check("safety", False, f"aborted: {e}")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("script completed", False, repr(e))
    finally:
        if start is not None:
            kitty("focus-window", "--match", f"id:{start}")
        if not a.keep:
            for wid in our_windows():
                kitty("close-window", "--match", f"id:{wid}")
            for name in list(OWNED.sessions):
                sh([ZJ, "kill-session", ours(name)])
                sh([ZJ, "delete-session", "--force", ours(name)])
            stop_substitutes()
            shutil.rmtree(work, ignore_errors=True)
            left = sorted(n for n in session_names() if n.startswith(PREFIX))
            check("cleanup: no sessions of this run left", not left, f"{left}")
        check("cleanup: focus restored to start window", start is None or focused_window() == start)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
