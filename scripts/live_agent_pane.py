#!/usr/bin/env python3
"""Live verification (`o` new agent pane, `g` lazygit floating pane) on disposable resources only.

Reuses scripts/live_spawn.py's harness (per-run random id, Ownership record, kitty focus restore):
a scratch repo `agentreetest-<id>-repo` (+ local bare origin) in a fresh temp dir with two worktrees,
`m4-tab` (has a tab in session a) and `m4-notab` (no tab); a session `agentreetest-<id>-a` attached
in a marked kitty tab; temp AGENTREE_STATE_DIR / AGENTREE_CONFIG; an inert opencode substitute
(`.opencode-agentreetest`, records its argv) as opencode_cmd and a lazygit substitute (records its
cwd, sleeps) as lazygit_cmd. Cleanup touches only sessions in the Ownership record and kitty
windows marked for this run.

Run from a kitty window, plain or inside a zellij session:  python3 scripts/live_agent_pane.py [--keep]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import live_spawn as h  # noqa: E402  (harness: RUN, PREFIX, OWNED, check, sh, zj, tabs, panes, kitty, ...)

check, wait, kitty, panes, tabs, clients = h.check, h.wait, h.kitty, h.panes, h.tabs, h.clients
A, ZJ, GIT = h.A, h.ZJ, h.GIT
MARK = "--agentreetest-marker"


def make_lazygit_substitute(work: str) -> tuple[str, str]:
    rec = os.path.join(work, "lazygit-cwd.txt")
    path = os.path.join(work, "bin", "lazygit-sub")
    with open(path, "w") as fh:
        fh.write(f"#!/bin/bash\npwd -P > {rec}\nexec /bin/sleep 600\n")
    os.chmod(path, 0o755)
    return path, rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    inside = os.environ.get("ZELLIJ_SESSION_NAME")
    if inside:
        # allowed: agentree's own commands then inherit ZELLIJ / ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID,
        # as they do in the real popup (regression: list-sessions env leak). The harness's own
        # zellij commands run with those variables removed; this session is never touched.
        print(f"NOTE  running inside zellij session {inside!r}: its ZELLIJ* env is inherited by agentree's "
              f"commands", flush=True)
    from agentree.sources import kitty as ksrc
    socks = ksrc.sockets()
    lo = os.environ.get("KITTY_LISTEN_ON")
    h.SOCK = lo if lo in socks else (socks[0] if len(socks) == 1 else "")
    if not h.SOCK:
        raise SystemExit(f"need exactly one owned kitty socket, found {socks}")
    h.LOCKED = h.screen_locked()
    work = os.path.realpath(tempfile.mkdtemp(prefix=f"agentreetest-{h.RUN}-"))
    state = os.path.join(work, "state")
    os.environ["AGENTREE_STATE_DIR"] = state
    os.environ["AGENTREE_CONFIG"] = os.path.join(work, "config.toml")
    start = h.focused_window()
    print(f"run {h.RUN}: work {work}; kitty {h.SOCK}; start window {start}" + ("; screen LOCKED" if h.LOCKED else ""),
          flush=True)
    try:
        repo = h.make_repo(work)
        oc, argv_file = h.make_agent_substitute(work)
        lgsub, lg_rec = make_lazygit_substitute(work)
        wt_tab = os.path.join(repo, ".worktrees", "m4-tab")
        wt_notab = os.path.join(repo, ".worktrees", "m4-notab")
        for p, b in ((wt_tab, "agent/m4-tab"), (wt_notab, "agent/m4-notab")):
            h.sh([*GIT, "-C", repo, "worktree", "add", "-q", "-b", b, p, "HEAD"], check_rc=True)
        with open(os.environ["AGENTREE_CONFIG"], "w") as fh:
            fh.write(f'repo_roots = ["{repo}"]\nzellij_socket_dirs = ["{h.DIR}"]\n'
                     f'opencode_cmd = "{oc} {MARK}"\nlazygit_cmd = "{lgsub}"\n')
        lay = os.path.join(work, "a.kdl")
        with open(lay, "w") as fh:
            fh.write(f'layout {{\n  tab name="main" cwd="{repo}" {{\n    pane\n  }}\n'
                     f'  tab name="m4-tab" cwd="{wt_tab}" {{\n    pane\n  }}\n}}\n')
        h.create_session(A, lay)
        r = kitty("launch", "--type=tab", "--tab-title", A, "--var", f"agentreetest_run={h.RUN}", "--var",
                  f"agentree_session={A}", "--env", f"ZELLIJ_SOCKET_DIR={h.DIR}", ZJ, "attach", A)
        wid_a = int(r.stdout.strip())
        check("setup: kitty tab attached to session a", wait(lambda: clients(A) == 1, 5), f"window {wid_a}")
        h.zj(A, "new-tab", "--name", "tmp")                     # make zellij set the window title
        h.zj(A, "close-tab")
        wait(lambda: any(w["id"] == wid_a and w["title"].startswith(f"{A} | ") for _o, _t, w in h.windows()), 5)
        kitty("focus-window", "--match", f"id:{start}")

        from agentree import state as st
        from agentree.actions.lazygit import MISSING
        from agentree.actions.real import RealActions
        from agentree.config import load_config
        from agentree.gather import gather
        from agentree.types import Row, WorktreeNode

        cfg = load_config()
        if cfg.warnings or not cfg.opencode_cmd.startswith(oc) or cfg.lazygit_cmd != lgsub:
            raise h.Abort(f"temp config not loaded: {cfg.warnings}")
        environ = {"ZELLIJ_SOCKET_DIR": h.DIR, "KITTY_LISTEN_ON": h.SOCK}

        def actions(c=cfg, **kw) -> RealActions:
            return RealActions(c, environ=environ, **kw)

        def snapshot():
            s = gather(cfg)
            names = set(h.OWNED.sessions)
            return dataclasses.replace(s, sessions=tuple(n for n in s.sessions if n.session.name in names))

        def wt_row(snap, path):
            for n in snap.sessions:
                for wn in n.worktrees:
                    if wn.worktree.path == path:
                        return Row(key=f"w:{n.session.name}:{path}", kind="worktree", depth=1, caret=None,
                                   icon=None, label=(), meta=(), ref=wn, session=n.session.name)
            for w in snap.orphans.worktrees:
                if w.path == path:              # "Needs a home": t / o / g use the chosen session
                    return Row(key=f"ow:{path}", kind="worktree", depth=1, caret=None, icon=None, label=(),
                               meta=(), ref=WorktreeNode(w), session=A)
            return None

        def tab_named(name):
            return next((t for t in tabs(A) if t["name"] == name), None)

        def tab_panes(tid):
            return [p for p in panes(A) if p["tab_id"] == tid]

        def real(p):
            return os.path.realpath(p.get("pane_cwd") or "/nonexistent")

        def focused_pane(tid):
            """The pane the (single) client has focused, per `list-clients` (list-panes' is_focused
            is per pane layer and not exclusive); only if it lies in tab `tid`."""
            rows = [ln.split() for ln in h.zj(A, "list-clients").stdout.splitlines()[1:] if ln.split()]
            ids = [int(r[1][len("terminal_"):]) for r in rows if len(r) > 1 and r[1].startswith("terminal_")]
            if len(ids) != 1:
                return None
            return ids[0] if any(p["id"] == ids[0] for p in tab_panes(tid)) else None

        def dbg(tid):
            return (f"window={h.focused_window()} tabs={[(t['tab_id'], t['name'], t.get('active')) for t in tabs(A)]} "
                    f"client pane={focused_pane(tid)} "
                    f"panes={[(p['id'], p.get('title'), p.get('is_floating')) for p in tab_panes(tid)]}")

        # ---- 0. dry-run o: commands logged, nothing created
        snap = snapshot()
        t1 = tab_named("m4-tab")
        before = {p["id"] for p in tab_panes(t1["tab_id"])} if t1 else set()
        rw = wt_row(snap, wt_tab)
        res = actions(dry_run=True).new_agent(rw, snap) if rw else None
        check("o dry-run: commands logged, nothing created",
              res and res.ok and res.message.startswith("dry-run:") and
              {p["id"] for p in tab_panes(t1["tab_id"])} == before, res.message if res else "no row")

        # ---- 1. o on a worktree that has a tab, while another tab ("main") is active: the pane is
        # placed with --tab-id and focused via go-to-tab-by-id + list-clients read-back
        tm = tab_named("main")
        if tm:
            h.zj(A, "go-to-tab-by-id", str(tm["tab_id"]))
        check("o (tab exists): precondition — tab main active",
              wait(lambda: bool(tab_named("main")) and tab_named("main").get("active"), 3), dbg(t1["tab_id"]))
        res = actions().new_agent(rw, snap) if rw else None
        check("o (tab exists): result ok + close", res and res.ok and res.close, res.message if res else "no row")
        try:
            log = open(os.path.join(state, "log")).read()
        except OSError:
            log = ""
        np_lines = [ln.strip() for ln in log.splitlines() if " new-pane " in ln and " DRY " not in ln]
        check("o (tab exists): new-pane used --tab-id <tab>",
              bool(np_lines) and f"new-pane --tab-id {t1['tab_id']} --cwd" in np_lines[-1],
              np_lines[-1][-200:] if np_lines else "no new-pane logged")
        new = [p for p in tab_panes(t1["tab_id"]) if p["id"] not in before]
        check("o (tab exists): one new pane in that tab, named opencode, cwd = worktree",
              len(new) == 1 and new[0].get("title") == "opencode" and real(new[0]) == wt_tab,
              f"{[(p['id'], p.get('title'), p.get('pane_cwd')) for p in new]}")
        got = wait(lambda: os.path.exists(argv_file) and json.load(open(argv_file)), 5)
        check("o (tab exists): substitute received its argv", got == [MARK], f"{got!r}")
        h.track_substitutes(os.path.join(work, "bin", ".opencode-agentreetest"))
        tt = tab_named("m4-tab")
        check("o (tab exists): tab active, pane focused, kitty window focused",
              bool(tt) and tt.get("active") and bool(new) and focused_pane(t1["tab_id"]) == new[0]["id"]
              and h.focused_window() == wid_a, dbg(t1["tab_id"]))
        rec = next((r for r in st.load_links() if r.get("worktreePath") == wt_tab), None)
        check("o (tab exists): links.json records the pane and its agent pid",
              rec and new and rec.get("session") == A and new[0]["id"] in (rec.get("agentPanes") or [])
              and len(rec.get("agentPids") or []) == 1, f"{rec}")
        kitty("focus-window", "--match", f"id:{start}")

        # ---- 2. o on a worktree without a tab: tab + agent
        os.unlink(argv_file)
        snap = snapshot()
        r2 = wt_row(snap, wt_notab)
        check("o (no tab): precondition — no tab yet", tab_named("m4-notab") is None)
        res = actions().new_agent(r2, snap) if r2 else None
        check("o (no tab): result ok + close", res and res.ok and res.close, res.message if res else "no row")
        t2 = tab_named("m4-notab")
        tp = tab_panes(t2["tab_id"]) if t2 else []
        oc_panes = [p for p in tp if p.get("title") == "opencode"]
        check("o (no tab): tab created with a shell pane + the agent pane, both in the worktree",
              bool(t2) and len(tp) == 2 and len(oc_panes) == 1 and all(real(p) == wt_notab for p in tp),
              f"{[(p['id'], p.get('title'), p.get('pane_cwd')) for p in tp]}")
        got = wait(lambda: os.path.exists(argv_file) and json.load(open(argv_file)), 5)
        check("o (no tab): substitute received its argv", got == [MARK], f"{got!r}")
        h.track_substitutes(os.path.join(work, "bin", ".opencode-agentreetest"))
        check("o (no tab): new tab active + agent pane focused",
              bool(t2) and tab_named("m4-notab").get("active") and bool(oc_panes)
              and focused_pane(t2["tab_id"]) == oc_panes[0]["id"], dbg(t2["tab_id"] if t2 else -1))
        kitty("focus-window", "--match", f"id:{start}")

        # ---- 3. g with the substitute: floating pane at the worktree
        snap = snapshot()
        rw = wt_row(snap, wt_tab)
        before = {p["id"] for p in tab_panes(t1["tab_id"])}
        res = actions().lazygit(rw, snap) if rw else None
        check("g: result ok + close", res and res.ok and res.close, res.message if res else "no row")
        new = [p for p in tab_panes(t1["tab_id"]) if p["id"] not in before]
        check("g: one floating pane named lazygit in the worktree's tab, cwd = worktree",
              len(new) == 1 and new[0].get("is_floating") and new[0].get("title") == "lazygit"
              and real(new[0]) == wt_tab, f"{[(p['id'], p.get('title'), p.get('is_floating'), p.get('pane_cwd')) for p in new]}")
        cwd = wait(lambda: os.path.exists(lg_rec) and open(lg_rec).read().strip(), 5)
        check("g: the command ran in the worktree", cwd == wt_tab, f"{cwd!r}")
        check("g: floating pane focused, kitty window focused",
              bool(new) and focused_pane(t1["tab_id"]) == new[0]["id"] and h.focused_window() == wid_a,
              dbg(t1["tab_id"]))
        kitty("focus-window", "--match", f"id:{start}")

        # ---- 4. g with a missing binary
        missing = dataclasses.replace(cfg, lazygit_cmd=os.path.join(work, "bin", "no-such-lazygit"))
        before = {p["id"] for p in panes(A)}
        res = actions(missing).lazygit(wt_row(snapshot(), wt_tab), snap)
        check("g (missing binary): the install message, nothing opened",
              res and not res.ok and res.message == MISSING and {p["id"] for p in panes(A)} == before,
              res.message if res else "")
        res = actions(dataclasses.replace(cfg, lazygit_cmd="lazygit")).lazygit(wt_row(snapshot(), wt_tab), snap)
        check("g (default lazygit, not installed here): the install message", res and res.message == MISSING,
              res.message if res else "")

        def link_of(path):
            return next((dict(r) for r in st.load_links() if r.get("worktreePath") == path), None)

        # ---- 5. links.json owned by another session (never created; same socket dir, other
        #         incarnation): o still opens the agent, the record stays exactly as it was
        ghost = h.PREFIX + "ghost"
        st.merge_link(wt_notab, lambda cur: {**(cur or {}), "session": ghost,
                                             "socketDir": os.path.realpath(h.DIR), "sessionStarted": 1.0})
        prev = link_of(wt_notab)
        snap = snapshot()
        res = actions().new_agent(wt_row(snap, wt_notab), snap)
        h.track_substitutes(os.path.join(work, "bin", ".opencode-agentreetest"))
        check("o (record of another session): agent opened, 'link kept' reported, record untouched",
              res and res.ok and f"link kept for {ghost}; agent not recorded" in res.message
              and link_of(wt_notab) == prev, f"{res.message if res else ''}; {link_of(wt_notab)}")

        # ---- 6. the session is renamed outside agentree (links.json not told): o verifies the rename
        #         (old name gone, same socket dir, same start time) and moves the record along
        new_name = h.PREFIX + "m4renamed"
        if new_name in h.session_names():
            raise h.Abort(f"{new_name} already exists")
        prev = link_of(wt_tab)
        h.zj(A, "rename-session", new_name)
        if wait(lambda: h.session_names().get(new_name), 5):
            h.OWNED.rename(A, new_name)
        names = h.session_names()
        check("setup: session renamed outside agentree", names.get(new_name) and A not in names,
              f"{sorted(n for n in names if n.startswith(h.PREFIX))}")
        snap = snapshot()
        res = actions().new_agent(wt_row(snap, wt_tab), snap)
        h.track_substitutes(os.path.join(work, "bin", ".opencode-agentreetest"))
        rec = link_of(wt_tab) or {}
        check("o after an external rename: verified, record moved to the new name with the new agent",
              res and res.ok and "link kept" not in res.message and rec.get("session") == new_name
              and prev is not None and set(prev.get("agentPanes") or []) < set(rec.get("agentPanes") or [])
              and abs((rec.get("sessionStarted") or 0) - (prev.get("sessionStarted") or -99)) <= 5,
              f"{res.message if res else ''}; before={prev}; after={rec}")

        # ---- 7. (inside zellij only, read-only) the session agentree runs in is looked up across both
        #         socket dirs without the "live in several socket dirs" false positive
        if inside:
            from agentree.actions.runner import canonical_dir
            from agentree.sources import zellij as zsrc
            other = os.path.join(os.path.realpath(os.environ.get("TMPDIR") or "/tmp"), f"zellij-{os.getuid()}")
            home = os.environ.get("ZELLIJ_SOCKET_DIR") or other
            dirs = list(dict.fromkeys([home, h.DIR, other]))
            leak = [d for d in dirs if canonical_dir(d) != canonical_dir(home) and any(
                ps.name == inside and not ps.exited for ps in zsrc.parse_list_sessions(subprocess.run(
                    [ZJ, "list-sessions", "-n"], env=dict(os.environ, ZELLIJ_SOCKET_DIR=d),
                    capture_output=True, text=True, timeout=10).stdout))]
            print(f"      (unwrapped list-sessions reports {inside} live in other dirs: {leak or 'none'})")
            nav = actions().navigator()
            nav.socket_dirs = lambda: dirs
            try:
                f = nav.lookup_session(inside)
                got = f"live in {f.live_dir}" if f and f.live_dir else f"{f}"
                ok = f is not None and f.live_dir is not None and canonical_dir(f.live_dir) == canonical_dir(home)
            except Exception as e:  # noqa: BLE001 - _Fail carries the message
                got, ok = f"refused: {e}", False
            check("lookup of the session agentree runs in: live in its own socket dir only", ok,
                  f"{inside}: {got}; dirs={dirs}")
    except h.Abort as e:
        check("safety", False, f"aborted: {e}")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("script completed", False, repr(e))
    finally:
        if start is not None:
            kitty("focus-window", "--match", f"id:{start}")
        if not a.keep:
            for wid in h.our_windows():
                kitty("close-window", "--match", f"id:{wid}")
            for name in list(h.OWNED.sessions):
                h.sh([ZJ, "kill-session", h.ours(name)])
                h.sh([ZJ, "delete-session", "--force", h.ours(name)])
            h.stop_substitutes()
            shutil.rmtree(work, ignore_errors=True)
            left = sorted(n for n in h.session_names() if n.startswith(h.PREFIX))
            check("cleanup: no sessions of this run left", not left, f"{left}")
        check("cleanup: focus restored to start window", start is None or h.focused_window() == start)
    failed = [n for n, ok, _ in h.RESULTS if not ok]
    print(f"\n{len(h.RESULTS) - len(failed)}/{len(h.RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    sys.exit(main())
