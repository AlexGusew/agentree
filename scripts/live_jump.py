#!/usr/bin/env python3
"""Live verification of jump and open tab on disposable resources only.

Every run gets a random id: sessions `agentreetest-<id>-a` (detached) and `agentreetest-<id>-b` (hosted
in a new kitty tab) in /tmp/zellij-<uid>, and a scratch repo (worktree .worktrees/feat-x)
inside a fresh temp dir. It then drives the real actions (jump, open tab, dry-run) and the
Alt+w popup keybind, verifying focus through `kitty @ ls` and `zellij action list-tabs/panes`.

Ownership: pre-existing sessions with the run's names abort the run; only the two sessions it
created are ever acted on or deleted; kitty windows are closed only after re-validating that
they carry this run's marker (user var `agentreetest_run=<id>`, or `agentree_session=<our session>` for
tabs agentree launched) and run `zellij attach <our session>`. Kitty focus returns to the start
window. Run from a kitty window, plain or inside a zellij session:
  python3 scripts/live_jump.py [--keep] [--no-popup]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import secrets
import shutil
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
A, B = PREFIX + "a", PREFIX + "b"
OURS = (A, B)
UID = os.getuid()
DIR = f"/tmp/zellij-{UID}"
ZJ = ex.which("zellij") or "zellij"
KT = ksrc.kitty_bin()
AGENTREE_BIN = os.path.expanduser("~/bin/agentree")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return bool(ok)


# --------------------------------------------------------------------------- shell helpers

def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZELLIJ")}
    env["ZELLIJ_SOCKET_DIR"] = DIR
    env.update(extra)
    return env


def sh(argv, *, env=None, cwd=None, check_rc=False) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, env=env or clean_env(), cwd=cwd, capture_output=True, text=True, timeout=20)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"{argv}: {r.returncode} {r.stderr.strip()}")
    return r


class Abort(Exception):
    """A safety check failed: stop before touching anything that may not be ours."""


def require_ours(name: str) -> str:
    if name not in OURS:
        raise Abort(f"refusing to act on session {name!r}: not created by this run")
    return name


def zj(session: str, *args: str) -> subprocess.CompletedProcess:
    require_ours(session)
    return sh([ZJ, "--session", session, "action", *args])


def tabs(session: str) -> list[dict]:
    r = zj(session, "list-tabs", "--all", "--json")
    return json.loads(r.stdout or "[]") if r.returncode == 0 else []


def panes(session: str) -> list[dict]:
    r = zj(session, "list-panes", "--tab", "--command", "--state", "--json")
    return [p for p in json.loads(r.stdout or "[]") if not p.get("is_plugin")] if r.returncode == 0 else []


def clients(session: str) -> int:
    r = zj(session, "list-clients")
    return sum(1 for ln in r.stdout.splitlines() if ln.split() and ln.split()[0].isdigit())


def client_pane(session: str) -> str | None:
    """The pane the single client focuses: list-clients' ZELLIJ_PANE_ID column (list-panes'
    is_focused is per layer and not exclusive, so it is never used as the truth)."""
    rows = [ln.split() for ln in zj(session, "list-clients").stdout.splitlines()]
    rows = [r for r in rows if r and r[0].isdigit()]
    return rows[0][1] if len(rows) == 1 and len(rows[0]) > 1 else None


def active_tab(session: str) -> dict | None:
    return next((t for t in tabs(session) if t.get("active")), None)


def owned_socket() -> str:
    socks = ksrc.sockets()
    lo = os.environ.get("KITTY_LISTEN_ON")
    if lo in socks:
        return lo
    if len(socks) == 1:
        return socks[0]
    raise SystemExit(f"need exactly one owned kitty socket, found {socks}")


SOCK = ""


def kitty(*args: str) -> subprocess.CompletedProcess:
    return sh([KT, "@", "--to", SOCK, *args], env=os.environ.copy())


def kls() -> list:
    return json.loads(kitty("ls").stdout or "[]")


def windows() -> list[tuple[dict, dict, dict]]:
    return [(ow, t, w) for ow in kls() for t in ow.get("tabs") or [] for w in t.get("windows") or []]


def screen_locked() -> bool:
    """macOS: True while the login session's screen is locked (then no window has OS focus)."""
    import plistlib

    try:
        r = subprocess.run(["ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, timeout=5)
        root = plistlib.loads(r.stdout)
    except Exception:  # noqa: BLE001
        return False
    return any(u.get("CGSSessionScreenIsLocked") for u in root.get("IOConsoleUsers") or [])


LOCKED = False      # set in main(); while locked, OS focus is unobservable -> kitty's active chain


def focused_window() -> int | None:
    """The window with OS focus (OS window, tab and window focused). While the screen is locked
    macOS gives no window focus, so kitty's own active OS-window/tab/window chain is used."""
    key = "is_active" if LOCKED else "is_focused"
    for ow, t, w in windows():
        if ow.get(key) and t.get(key) and w.get(key):
            return w["id"]
    return None


def window(wid: int) -> dict | None:
    return next((w for _ow, _t, w in windows() if w["id"] == wid), None)


def zellij_client_windows(session: str) -> list[int]:
    out = []
    for _ow, _t, w in windows():
        for p in w.get("foreground_processes") or []:
            c = p.get("cmdline") or []
            if c and os.path.basename(c[0]) == "zellij" and session in c:
                out.append(w["id"])
                break
    return out


def select_our_windows(ls_data: list, run: str, ours) -> list[int]:
    """Kitty windows a run may close (pure; `ls_data` = parsed `kitty @ ls`).

    A window qualifies only if it is marked as ours (user var `agentreetest_run=<run>`, or
    `agentree_session=<one of our sessions>` set by agentree on tabs it launched) AND the kitty
    corroboration rules of agentree.sources.kitty resolve its *current* host to one of our sessions:
    title_overridden rejects; a title naming any other session rejects (e.g. the client switched
    to a user session); kitty's default title is accepted only for a freshly launched client whose
    `agentree_session` var equals its `zellij attach <S>` argv and whose title names no session."""
    ours = set(ours)
    out = []
    for ow in ls_data if isinstance(ls_data, list) else []:
        for t in ow.get("tabs") or []:
            for w in t.get("windows") or []:
                uv = w.get("user_vars") or {}
                marked = uv.get("agentreetest_run") == run or uv.get("agentree_session") in ours
                if marked and ksrc.resolve_window(w, ours) in ours:
                    out.append(w["id"])
    return out


def our_windows() -> list[int]:
    """Re-validated against a fresh `kitty @ ls` right before closing."""
    return select_our_windows(kls(), RUN, OURS)


def wait(pred, timeout: float = 5.0, step: float = 0.1):
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v or time.monotonic() >= deadline:
            return v
        time.sleep(step)


# --------------------------------------------------------------------------- setup / teardown

def make_repo(base: str) -> tuple[str, str]:
    repo = os.path.join(base, "agentreetest-repo")
    if os.path.exists(repo):
        raise Abort(f"{repo} already exists")
    os.makedirs(repo)
    g = ["git", "-c", "user.name=agentreetest", "-c", "user.email=agentreetest@example.invalid"]
    sh([*g, "init", "-q", "-b", "main"], cwd=repo, check_rc=True)
    with open(os.path.join(repo, "README"), "w") as fh:
        fh.write("scratch\n")
    sh([*g, "add", "README"], cwd=repo, check_rc=True)
    sh([*g, "commit", "-q", "-m", "init"], cwd=repo, check_rc=True)
    with open(os.path.join(repo, ".git", "info", "exclude"), "a") as fh:
        fh.write(".worktrees/\n")
    wt = os.path.join(repo, ".worktrees", "feat-x")
    sh([*g, "worktree", "add", "-q", "-b", "agent/feat-x", wt], cwd=repo, check_rc=True)
    return os.path.realpath(repo), os.path.realpath(wt)


def layout(path: str, tabs_kdl: str) -> str:
    with open(path, "w") as fh:
        fh.write("layout {\n" + tabs_kdl + "}\n")
    return path


class Ownership:
    """Explicit record of what this run created. Cleanup only ever touches what is in here; a
    session is registered only after the pre-existence check passed and this run's create
    command produced it."""

    def __init__(self) -> None:
        self.sessions: list[str] = []

    def owns(self, name: str) -> bool:
        return name in self.sessions


def kill_session(name: str, owned: Ownership, sh=sh) -> None:
    require_ours(name)
    if not owned.owns(name):
        raise Abort(f"refusing to delete session {name!r}: this run did not create it")
    sh([ZJ, "kill-session", name])
    sh([ZJ, "delete-session", name, "--force"])


def all_session_names(sh=sh) -> set[str]:
    names: set[str] = set()
    for d in {DIR, os.path.join(os.path.realpath(os.environ.get("TMPDIR") or "/tmp"), f"zellij-{UID}")}:
        r = sh([ZJ, "list-sessions", "-n", "-s"], env=clean_env(ZELLIJ_SOCKET_DIR=d))
        names |= set((r.stdout or "").split())
    return names


def create_session(name: str, layout_path: str, owned: Ownership, sh=sh) -> None:
    """Create a background session; register it as ours only once it is known that it did not
    exist before and our command created it (also when the command failed half-way)."""
    require_ours(name)
    if name in all_session_names(sh):
        raise Abort(f"session {name} already exists — not ours, refusing")
    r = sh([ZJ, "attach", "--create-background", name, "options", "--default-layout", layout_path])
    if r.returncode == 0 or name in all_session_names(sh):
        owned.sessions.append(name)
    if r.returncode != 0:
        raise RuntimeError(f"creating {name} failed: {(r.stderr or '').strip()[:200]}")


def cleanup_sessions(owned: Ownership, sh=sh) -> None:
    for name in list(owned.sessions):
        kill_session(name, owned, sh)


# --------------------------------------------------------------------------- main

def main() -> int:
    global SOCK, LOCKED
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true", help="leave sessions/repo/tabs for inspection")
    ap.add_argument("--no-popup", action="store_true", help="skip the Alt+w popup check")
    a = ap.parse_args()
    inside = os.environ.get("ZELLIJ_SESSION_NAME")
    if inside:
        # allowed: agentree's own commands then inherit ZELLIJ / ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID,
        # as they do in the real popup (regression: list-sessions env leak). The harness's own
        # zellij commands run with those variables removed; this session is never touched.
        print(f"NOTE  running inside zellij session {inside!r}: its ZELLIJ* env is inherited by agentree's "
              f"commands", flush=True)
    SOCK = owned_socket()
    LOCKED = screen_locked()
    if LOCKED:
        print("NOTE  screen is locked: focus checks use kitty's active window chain (OS focus is not "
              "observable while locked)", flush=True)
    work = os.path.realpath(tempfile.mkdtemp(prefix=f"agentreetest-{RUN}-"))
    state = os.path.join(work, "state")
    os.environ["AGENTREE_STATE_DIR"] = state          # cache/log/locks of this run (and its popups) stay here
    start = focused_window()
    owned = Ownership()
    print(f"run {RUN}: sessions {A}, {B}; kitty socket {SOCK}, start window {start}, zellij dir {DIR}",
          flush=True)
    try:
        repo, wt = make_repo(work)
        create_session(A, layout(os.path.join(work, "a.kdl"),
                                 f'  tab name="main" cwd="{repo}" {{ pane; }}\n'
                                 f'  tab name="scratch" cwd="/tmp" {{ pane; }}\n'), owned)
        create_session(B, layout(os.path.join(work, "b.kdl"),
                                 f'  tab name="main" cwd="{repo}" {{ pane; }}\n'
                                 f'  tab name="feat" cwd="{wt}" {{\n    pane split_direction="vertical" {{\n      pane\n      pane\n    }}\n  }}\n'
                                 f'  tab name="three" cwd="{repo}" {{\n    pane split_direction="vertical" {{\n'
                                 f'      pane focus=true\n      pane\n      pane\n    }}\n  }}\n'), owned)
        for s_ in (A, B):
            if not wait(lambda: len(tabs(s_)) > 0, 5):
                raise RuntimeError(f"{s_} did not come up")
        r = kitty("launch", "--type=tab", "--tab-title", B, "--var", f"agentreetest_run={RUN}", "--var", f"agentree_session={B}",
                  "--env", f"ZELLIJ_SOCKET_DIR={DIR}", ZJ, "attach", B)
        wid_b = int(r.stdout.strip())
        check("setup: kitty tab attached to agentreetest-b", wait(lambda: clients(B) == 1, 5), f"window {wid_b}")
        tb = {t["name"]: t["tab_id"] for t in tabs(B)}
        # zellij sets the terminal title only when a pane title changes: switch tabs once
        zj(B, "go-to-tab-by-id", str(tb["feat"]))
        zj(B, "go-to-tab-by-id", str(tb["main"]))
        titled = wait(lambda: (window(wid_b) or {}).get("title", "").startswith(f"{B} | "), 5)
        check("setup: agentreetest-b window title is '<session> | …'", titled, repr((window(wid_b) or {}).get("title")))
        kitty("focus-window", "--match", f"id:{start}")

        from agentree.actions.real import RealActions
        from agentree.config import Config
        from agentree.gather import gather
        from agentree.types import Row, WorktreeNode

        cfg = Config(repo_roots=(repo,), zellij_socket_dirs=(DIR,))
        environ = {"ZELLIJ_SOCKET_DIR": DIR, "KITTY_LISTEN_ON": SOCK}

        def snapshot():
            s = gather(cfg)
            return dataclasses.replace(s, sessions=tuple(n for n in s.sessions if n.session.name in OURS))

        def node(snap, name):
            return next(n for n in snap.sessions if n.session.name == name)

        def pane_row(snap, name, tab_name, index=-1):
            n = node(snap, name)
            t = next(t for t in n.session.tabs if t.name == tab_name)
            p = [p for p in t.panes if not p.is_plugin][index]
            return Row(key=f"p:{name}:{p.zid}", kind="pane", depth=2, caret=None, icon=None, label=(), meta=(),
                       ref=p, session=name), p

        actions = RealActions(cfg, environ=environ)
        snap = snapshot()
        nb = node(snap, B)
        check("snapshot: agentreetest-b host detected", nb.kitty is not None and nb.kitty.window_id == wid_b,
              f"host={nb.kitty}")
        check("snapshot: agentreetest-a detached", node(snap, A).session.clients == 0)

        def log_lines() -> list[str]:
            try:
                with open(os.path.join(state, "log")) as fh:
                    return fh.readlines()
            except OSError:
                return []

        # ---- 1. jump to a pane of agentreetest-b (other tab, other kitty tab)
        row_b, pb = pane_row(snap, B, "feat")
        res = actions.jump(row_b, snap)
        time.sleep(0.3)
        at = active_tab(B)
        check("jump agentreetest-b pane: result ok+close", res.ok and res.close and "could not focus" not in res.message,
              res.message)
        check("jump agentreetest-b pane: hosting kitty window focused", focused_window() == wid_b,
              f"focused={focused_window()}")
        check("jump agentreetest-b pane: target tab active", at is not None and at["name"] == "feat", f"active={at and at['name']}")
        check("jump agentreetest-b pane: target pane focused (list-clients)", client_pane(B) == pb.zid,
              f"target {pb.zid}, client on {client_pane(B)}")

        # ---- 2. already focused: idempotent
        raw = zj(B, "focus-pane-id", pb.zid)
        print(f"      (raw focus-pane-id on focused pane: rc={raw.returncode} stderr={raw.stderr.strip()!r})")
        res2 = actions.jump(row_b, snap)
        check("jump again (already focused): ok", res2.ok and res2.close, res2.message)
        check("jump again: still focused", focused_window() == wid_b and (active_tab(B) or {}).get("name") == "feat"
              and client_pane(B) == pb.zid, f"client on {client_pane(B)}")

        # ---- 2b. tab of 3 tiled panes: jump to the middle one (not focused). focus-pane-id is a
        #          no-op for tiled panes in 0.45.1, so this needs the focus-next-pane walk.
        zj(B, "go-to-tab-by-id", str(tb["three"]))
        time.sleep(0.2)
        first3 = client_pane(B)
        zj(B, "go-to-tab-by-id", str(tb["main"]))
        time.sleep(0.2)
        kitty("focus-window", "--match", f"id:{start}")
        row_m, pm = pane_row(snap, B, "three", 1)
        n_log = len(log_lines())
        res = actions.jump(row_m, snap)
        time.sleep(0.3)
        walked = [ln for ln in log_lines()[n_log:] if " focus-next-pane" in ln]
        check("jump to middle of 3 tiled panes: precondition — another pane focused in that tab",
              first3 is not None and first3 != pm.zid, f"first={first3} middle={pm.zid}")
        check("jump to middle of 3 tiled panes: result ok+close, no focus note",
              res.ok and res.close and "could not focus" not in res.message, res.message)
        check("jump to middle of 3 tiled panes: tab active + middle pane focused (list-clients)",
              (active_tab(B) or {}).get("name") == "three" and client_pane(B) == pm.zid,
              f"client on {client_pane(B)}; {len(walked)} focus-next-pane step(s)")
        check("jump to middle of 3 tiled panes: kitty window focused", focused_window() == wid_b)

        # ---- 2c. popup case: agentree's own floating pane is focused in the target tab. focus-next-pane
        #          never leaves a shown floating layer, so agentree hides it (toggle-floating-panes) first.
        fl = zj(B, "new-pane", "--floating", "--name", "fake-agentree", "--", "/bin/sleep", "600").stdout.strip()
        time.sleep(0.5)
        row_l, pl = pane_row(snap, B, "three", 2)
        popup_env = dict(environ, ZELLIJ_SESSION_NAME=B, ZELLIJ_PANE_ID=fl.rsplit("_", 1)[-1])
        on_popup = client_pane(B)
        n_log = len(log_lines())
        res = RealActions(cfg, environ=popup_env).jump(row_l, snap)
        time.sleep(0.3)
        hidden = not (active_tab(B) or {}).get("are_floating_panes_visible", True)
        seq = [c for ln in log_lines()[n_log:] for c in ("toggle-floating-panes", "focus-pane-id")
               if f" {c}" in ln]
        check("jump from own floating popup: tracked toggle before any focus-pane-id",
              bool(seq) and seq[0] == "toggle-floating-panes" and seq.count("toggle-floating-panes") == 1,
              f"{seq}")
        check("jump from own floating popup: precondition — the popup pane is focused", on_popup == fl,
              f"client on {on_popup}, popup {fl}")
        check("jump from own floating popup: ok, floating layer hidden, target focused (list-clients)",
              res.ok and "could not focus" not in res.message and hidden and client_pane(B) == pl.zid,
              f"{res.message}; hidden={hidden}; client on {client_pane(B)}, target {pl.zid}")

        # ---- 2d. the user's floating pane shares the shown layer with agentree's popup. A tiled target
        #          would need focus-pane-id or a toggle, and both hide the whole layer, so agentree sends
        #          neither: it reports (ok=False, popup kept open) and the layer stays shown.
        uf = zj(B, "new-pane", "--floating", "--name", "user-float", "--", "/bin/sleep", "600").stdout.strip()
        time.sleep(0.5)
        zj(B, "focus-pane-id", fl)
        time.sleep(0.2)
        # probe (not a check): what a raw focus-pane-id on a tiled pane does from the floating layer
        zj(B, "focus-pane-id", pm.zid)
        time.sleep(0.2)
        print(f"      (raw focus-pane-id {pm.zid} from floating {fl}: layer visible="
              f"{(active_tab(B) or {}).get('are_floating_panes_visible')}, client on {client_pane(B)})")
        if not (active_tab(B) or {}).get("are_floating_panes_visible"):
            zj(B, "toggle-floating-panes")
        zj(B, "focus-pane-id", fl)
        time.sleep(0.2)
        shown = (active_tab(B) or {}).get("are_floating_panes_visible")
        on_popup = client_pane(B)
        n_log = len(log_lines())
        res = RealActions(cfg, environ=popup_env).jump(row_m, snap)
        time.sleep(0.3)
        mine = log_lines()[n_log:]
        sent = [c for c in ("focus-pane-id", "toggle-floating-panes", "focus-next-pane")
                if any(f" {c}" in ln for ln in mine)]
        vis = (active_tab(B) or {}).get("are_floating_panes_visible")
        check("jump from popup with a user floating pane: precondition — layer shown, popup focused",
              bool(uf) and shown is True and on_popup == fl, f"uf={uf} shown={shown} client on {on_popup}")
        check("jump from popup with a user floating pane: reported (ok=False, close=False, hint)",
              not res.ok and not res.close and f"could not focus pane {pm.zid}" in res.message
              and "floating panes are shown in this tab" in res.message and "Ctrl Alt f" in res.message,
              res.message)
        check("jump from popup with a user floating pane: no focus-pane-id / toggle / walk sent", not sent,
              f"sent={sent}")
        check("jump from popup with a user floating pane: the floating layer is still shown, popup focused",
              vis is True and client_pane(B) == fl
              and any(p["id"] == int(uf.rsplit("_", 1)[-1]) and p.get("is_floating") for p in panes(B)),
              f"layer visible={vis}; client on {client_pane(B)}")
        if vis:
            zj(B, "toggle-floating-panes")             # hide the layer again for the steps below
            time.sleep(0.2)

        # ---- 3. dry-run: logs, changes nothing
        kitty("focus-window", "--match", f"id:{start}")
        zj(B, "go-to-tab-by-id", str(tb["main"]))
        time.sleep(0.2)
        dres = RealActions(cfg, dry_run=True, environ=environ).jump(row_b, snap)
        time.sleep(0.2)
        with open(os.path.join(state, "log")) as fh:
            dry_lines = [ln for ln in fh if " DRY " in ln]
        check("dry-run: message", dres.ok and dres.message.startswith("dry-run: 3 commands"), dres.message)
        check("dry-run: no focus change", focused_window() == start and (active_tab(B) or {}).get("name") == "main")
        check("dry-run: exact commands logged", len(dry_lines) == 3 and "focus-window --match id:" in dry_lines[-1],
              dry_lines[-1].strip() if dry_lines else "")

        # ---- 4. jump to detached agentreetest-a (launch kitty tab, attach, then navigate)
        row_a, pa = pane_row(snap, A, "scratch")
        before = set(zellij_client_windows(A))
        res = actions.jump(row_a, snap)
        time.sleep(0.3)
        new_a = [w for w in zellij_client_windows(A) if w not in before]
        check("jump detached agentreetest-a: result ok+close", res.ok and res.close, res.message)
        check("jump detached agentreetest-a: new kitty tab with zellij client", len(new_a) == 1 and clients(A) == 1,
              f"windows={new_a} clients={clients(A)}")
        check("jump detached agentreetest-a: new window focused", bool(new_a) and focused_window() == new_a[0])
        check("jump detached agentreetest-a: target tab active", (active_tab(A) or {}).get("name") == "scratch",
              f"active={(active_tab(A) or {}).get('name')}")

        # ---- 5. open tab for feat-x in agentreetest-a, twice
        kitty("focus-window", "--match", f"id:{start}")
        snap = snapshot()
        na = node(snap, A)
        check("snapshot: agentreetest-a host = launched window (agentree_session var)",
              na.kitty is not None and bool(new_a) and na.kitty.window_id == new_a[0], f"host={na.kitty}")
        wt_obj = next((wn.worktree for n in snap.sessions for wn in n.worktrees if wn.worktree.path == wt), None) \
            or next(w for w in snap.orphans.worktrees if w.path == wt)
        row_t = Row(key=f"w:{A}:{wt}", kind="worktree", depth=1, caret=None, icon=None, label=(), meta=(),
                    ref=WorktreeNode(wt_obj), session=A)

        def wt_tabs():
            ids = {p["tab_id"] for p in panes(A) if os.path.realpath(p.get("pane_cwd") or "/") == wt}
            return [t for t in tabs(A) if t["tab_id"] in ids]

        res = actions.open_tab(row_t, snap)
        time.sleep(0.3)
        made = wt_tabs()
        check("open tab feat-x: result ok+close", res.ok and res.close, res.message)
        check("open tab feat-x: one tab with cwd = worktree", len(made) == 1, f"{[t['name'] for t in made]}")
        check("open tab feat-x: new tab active + kitty window focused",
              bool(made) and (active_tab(A) or {}).get("tab_id") == made[0]["tab_id"]
              and bool(new_a) and focused_window() == new_a[0])
        zj(A, "go-to-tab-by-id", str(next(t["tab_id"] for t in tabs(A) if t["name"] == "main")))
        res = actions.open_tab(row_t, snap)       # stale snapshot on purpose: re-validation must catch it
        time.sleep(0.3)
        check("open tab again: no duplicate", len(wt_tabs()) == 1 and "already open" in res.message, res.message)
        check("open tab again: jumped to the existing tab",
              bool(made) and (active_tab(A) or {}).get("tab_id") == made[0]["tab_id"])

        # ---- 6. popup keybind + single instance (real ~/bin/agentree inside agentreetest-b)
        if not a.no_popup and os.path.exists(AGENTREE_BIN):
            kitty("focus-window", "--match", f"id:{wid_b}")
            time.sleep(0.3)

            def agentree_panes():
                return [p for p in panes(B) if p.get("is_floating") and not p.get("exited")
                        and ("agentree" in (p.get("terminal_command") or "") or p.get("title") == "agentree")]

            kitty("send-key", "--match", f"id:{wid_b}", "alt+w")
            first = wait(agentree_panes, 6)
            check("popup: Alt+w opens a floating agentree pane", bool(first),
                  f"{[(p['id'], p.get('title'), p.get('terminal_command')) for p in first or []]}")
            geo = sh([ZJ, "--session", B, "action", "list-panes", "--geometry", "--json"])
            g = next((p for p in json.loads(geo.stdout or "[]") if first and p.get("id") == first[0]["id"]
                      and not p.get("is_plugin")), {})
            print(f"      (popup geometry: { {k: v for k, v in g.items() if k in ('pane_x', 'pane_y', 'pane_columns', 'pane_rows', 'x', 'y', 'columns', 'rows', 'geometry')} })")
            time.sleep(1.5)                       # let the first instance claim the session
            # the popup inherits AGENTREE_STATE_DIR from the agentreetest-b server env: its state is `state`
            from agentree.actions.runner import identity_key
            inst = os.path.join(state, f"instance-{identity_key(DIR, B)}.json")
            rec = json.load(open(inst)) if os.path.exists(inst) else {}
            check("popup: instance record written", bool(first) and rec.get("pane_id") == str(first[0]["id"]),
                  f"{rec}")
            kitty("send-key", "--match", f"id:{wid_b}", "alt+w")
            time.sleep(2.5)
            check("popup: second Alt+w keeps a single instance", len(agentree_panes()) == 1, f"{len(agentree_panes())} agentree panes")
            with open(os.path.join(state, "log")) as fh:
                handoff = [ln.strip() for ln in fh if first and f"focus-pane-id terminal_{first[0]['id']}" in ln]
            check("popup: second instance focused the first and exited", bool(handoff), handoff[-1] if handoff else "")
            kitty("send-key", "--match", f"id:{wid_b}", "q")
            gone = wait(lambda: not agentree_panes(), 5)
            check("popup: q closes it (close_on_exit)", bool(gone))
            check("popup: instance record removed on exit", not os.path.exists(inst))

            # ---- 7. the real TUI path: popup in agentreetest-b, filter to agentreetest-a, Enter = jump
            kitty("send-key", "--match", f"id:{wid_b}", "alt+w")
            pop = wait(agentree_panes, 6)
            time.sleep(1.0)
            kitty("send-text", "--match", f"id:{wid_b}", f"/{A}")
            kitty("send-key", "--match", f"id:{wid_b}", "enter")          # leave filter mode
            time.sleep(0.8)
            screen = sh([ZJ, "--session", B, "action", "dump-screen", "--pane-id",
                         f"terminal_{pop[0]['id']}"]).stdout if pop else ""
            real = set()
            for d in {DIR, os.path.join(os.path.realpath(os.environ.get("TMPDIR") or "/tmp"), f"zellij-{UID}")}:
                r = sh([ZJ, "list-sessions", "-n", "-s"], env=clean_env(ZELLIJ_SOCKET_DIR=d))
                real |= {x for x in r.stdout.split() if x not in OURS}
            leaked = sorted(n for n in real if n in screen)
            safe = bool(pop) and A in screen and not leaked
            check("popup jump: filtered view shows only agentreetest-a (safety guard)", safe, f"leaked={leaked}")
            if safe:
                kitty("send-key", "--match", f"id:{wid_b}", "enter")      # jump
                closed = wait(lambda: not agentree_panes(), 5)
                time.sleep(0.3)
                check("popup jump: popup closed after jump", bool(closed))
                check("popup jump: agentreetest-a kitty window focused", bool(new_a) and focused_window() == new_a[0],
                      f"focused={focused_window()}")
            else:
                kitty("send-key", "--match", f"id:{wid_b}", "q")
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
            skipped = []
            for wid in our_windows():
                kitty("close-window", "--match", f"id:{wid}")
            for _ow, _t, w in windows():          # anything still attached to our sessions but unmarked
                if any(s_ in (p.get("cmdline") or []) for p in w.get("foreground_processes") or [] for s_ in OURS):
                    skipped.append(w["id"])
            cleanup_sessions(owned)
            shutil.rmtree(work, ignore_errors=True)
            if skipped:
                print(f"      (left unmarked kitty windows alone: {skipped})")
        left = sorted(all_session_names() & set(OURS))
        check("cleanup: no sessions of this run left", a.keep or not left, f"{left}")
        check("cleanup: focus restored to start window", start is None or focused_window() == start)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
