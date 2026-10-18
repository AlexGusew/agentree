#!/usr/bin/env python3
"""Live Claude Code verification (spawn with agent=claude, `o` with provider claude, hooks -> status) on disposable
resources only. The real claude CLI is never run.

Reuses scripts/live_spawn.py's harness (per-run random id, Ownership record, kitty focus restore):
a scratch repo `agentreetest-<id>-repo` (+ local bare origin) in a fresh temp dir; a session
`agentreetest-<id>-a` attached in a marked kitty tab; temp AGENTREE_STATE_DIR / AGENTREE_CONFIG. The claude
stand-in is a compiled sleeper named `claude` (so agentree's process scan sees an interactive claude:
argv0 basename `claude`) that writes its argv (NUL-separated) to a file, then sleeps. Claude's
hooks are emulated by running the command from the generated settings.json with hook JSON on
stdin and the stand-in pane's zellij environment. Cleanup touches only sessions in the Ownership
record and kitty windows marked for this run.

Run from a kitty window, plain or inside a zellij session:  python3 scripts/live_claude.py [--keep]
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
PROMPT = '-starts with a dash, "quoted"\nsecond line \\ backslash'


def make_fake_claude(work: str) -> tuple[str, str]:
    """bin/claude: records argv (NUL-separated) to claude-argv.bin, then sleeps."""
    bindir = os.path.join(work, "bin")
    os.makedirs(bindir, exist_ok=True)
    rec = os.path.join(work, "claude-argv.bin")
    src = os.path.join(work, "fake_claude.c")
    with open(src, "w") as fh:
        fh.write('#include <stdio.h>\n#include <unistd.h>\n'
                 'int main(int argc, char **argv){FILE *f = fopen(REC, "wb");\n'
                 ' if (f) { for (int i = 0; i < argc; i++) { fputs(argv[i], f); fputc(0, f); } fclose(f); }\n'
                 ' sleep(600); return 0;}\n')
    path = os.path.join(bindir, "claude")
    h.sh(["cc", f'-DREC="{rec}"', "-o", path, src], check_rc=True)
    return path, rec


def read_argv(rec: str) -> list[str] | None:
    try:
        with open(rec, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    return [x.decode() for x in data.split(b"\0")[:-1]] or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    inside = os.environ.get("ZELLIJ_SESSION_NAME")
    if inside:
        print(f"NOTE  running inside zellij session {inside!r}: it is never touched", flush=True)
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
    os.environ.pop("AGENTREE_SELF", None)
    start = h.focused_window()
    print(f"run {h.RUN}: work {work}; kitty {h.SOCK}; start window {start}" + ("; screen LOCKED" if h.LOCKED else ""),
          flush=True)
    try:
        repo = h.make_repo(work)
        fake, argv_rec = make_fake_claude(work)
        with open(os.environ["AGENTREE_CONFIG"], "w") as fh:
            fh.write(f'repo_roots = ["{repo}"]\nzellij_socket_dirs = ["{h.DIR}"]\n'
                     f'claude_cmd = "{fake}"\ndefault_agent = "claude"\nopencode_cmd = "/nonexistent/opencode"\n')
        lay = os.path.join(work, "a.kdl")
        with open(lay, "w") as fh:
            fh.write(f'layout {{\n  tab name="main" cwd="{repo}" {{\n    pane\n  }}\n}}\n')
        h.create_session(A, lay)
        r = kitty("launch", "--type=tab", "--tab-title", A, "--var", f"agentreetest_run={h.RUN}", "--var",
                  f"agentree_session={A}", "--env", f"ZELLIJ_SOCKET_DIR={h.DIR}", ZJ, "attach", A)
        wid_a = int(r.stdout.strip())
        check("setup: kitty tab attached to session a", wait(lambda: clients(A) == 1, 5), f"window {wid_a}")
        kitty("focus-window", "--match", f"id:{start}")

        from agentree.providers import claude as cc
        from agentree import state as st
        from agentree.actions.api import SpawnRequest
        from agentree.actions.real import RealActions
        from agentree.config import load_config
        from agentree.gather import gather
        from agentree.sources import proc
        from agentree.types import Row

        cfg = load_config()
        if cfg.warnings or cfg.claude_cmd != fake or cfg.default_agent != "claude":
            raise h.Abort(f"temp config not loaded: {cfg.warnings}")
        if not cc.settings_path().startswith(state):
            raise h.Abort(f"claude settings would go to {cc.settings_path()}")
        environ = {"ZELLIJ_SOCKET_DIR": h.DIR, "KITTY_LISTEN_ON": h.SOCK}

        def actions(**kw) -> RealActions:
            return RealActions(cfg, environ=environ, **kw)

        def snapshot():
            s = gather(cfg)
            names = set(h.OWNED.sessions)
            return dataclasses.replace(s, sessions=tuple(n for n in s.sessions if n.session.name in names))

        slug = "cc-x"
        wt = os.path.join(repo, ".worktrees", slug)
        req = SpawnRequest(repo=repo, branch=f"agent/{slug}", target="tab", prompt=PROMPT, agent="claude",
                           copy_env=False, run_install=False, switch_after=False)

        # ---- 0. options + dry run: the plan shows the claude command; nothing is written
        opts = actions().spawn_options(None, snapshot())
        check("spawn options: default agent from config", opts.default_provider == "claude", f"{opts.default_provider}")
        plan = actions(dry_run=True).plan_spawn(req, snapshot())
        text = "\n".join(" ".join(s) for s in plan.steps)
        check("plan: no errors, claude settings step, claude command in the layout",
              not plan.errors and "write claude hook settings" in text and f'command="{fake}"' in text
              and plan.data.get("launch_session_id") in text, f"{plan.errors} {plan.warnings}")
        res = actions(dry_run=True).execute(plan)
        check("dry-run: ok, no settings file, no worktree",
              res.ok and not os.path.exists(cc.settings_path()) and not os.path.exists(wt), res.message)

        # ---- 1. spawn with agent=claude and a prompt
        snap = snapshot()
        plan = actions().plan_spawn(req, snap)
        sid = plan.data.get("launch_session_id")
        check("plan (real): no errors", not plan.errors and bool(sid), f"{plan.errors}")
        res = actions().execute(plan)
        check("spawn: ok", res.ok, res.message)
        argv = wait(lambda: read_argv(argv_rec), 8)
        h.track_substitutes(fake)
        want = [fake, "--settings", cc.settings_path(), "--session-id", sid, "--", PROMPT]
        check("spawn: claude got --settings <state>/claude/settings.json --session-id <sid> -- <prompt>",
              argv == want, f"{argv!r}")
        check("spawn: hooks settings file written", os.path.isfile(cc.settings_path()))
        pids = [pid for pid in cc.find_claude(proc.list_procs())
                if cc.claude_session_arg((proc.proc_args_env(pid) or ((),))[0]) == sid]
        check("find_claude: the stand-in is an interactive claude with our session id", len(pids) == 1, f"{pids}")
        pid = pids[0] if pids else None
        env = (proc.proc_args_env(pid) or ((), {}))[1] if pid else {}
        check("stand-in runs in a pane of session a", env.get("ZELLIJ_SESSION_NAME") == A
              and (env.get("ZELLIJ_PANE_ID") or "").isdigit(), f"{ {k: v for k, v in env.items() if k.startswith('ZELLIJ')} }")
        rec = next((r for r in st.load_links() if r.get("worktreePath") == wt), None)
        check("links.json: agent pid recorded", rec is not None and pid in (rec.get("agentPids") or []), f"{rec}")

        # ---- 2. emulate Claude's hooks with the generated command
        with open(cc.settings_path()) as fh:
            doc = json.load(fh)
        hook = doc["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        henv = h.clean_env(AGENTREE_STATE_DIR=state, ZELLIJ_SESSION_NAME=env.get("ZELLIJ_SESSION_NAME", ""),
                           ZELLIJ_PANE_ID=env.get("ZELLIJ_PANE_ID", ""))
        rcs = []
        for ev in ("SessionStart", "UserPromptSubmit"):
            payload = {"session_id": sid, "hook_event_name": ev, "cwd": wt, "transcript_path": "",
                       "prompt": PROMPT, "source": "startup"}
            p = subprocess.run(["/bin/sh", "-c", hook], input=json.dumps(payload), capture_output=True, text=True,
                               env=henv, cwd=wt, timeout=30)
            rcs.append((p.returncode, p.stdout, p.stderr))
        record = cc.read_record(sid) or {}
        check("hook command: exit 0, silent, status record busy with the pane env",
              all(r == (0, "", "") for r in rcs) and record.get("status") == "busy"
              and record.get("zellij_session") == A and str(record.get("zellij_pane")) == env.get("ZELLIJ_PANE_ID"),
              f"{rcs}; {record}")
        p = subprocess.run(["/bin/sh", "-c", hook], input="garbage", capture_output=True, text=True, env=henv,
                           timeout=30)
        check("hook command: garbage input -> exit 0", p.returncode == 0 and not p.stdout, p.stderr[-200:])

        # ---- 3. agentree --json: a claude agent, from hooks, in the spawned worktree
        jenv = h.clean_env(AGENTREE_STATE_DIR=state, AGENTREE_CONFIG=os.environ["AGENTREE_CONFIG"], PYTHONPATH=ROOT)
        out = subprocess.run([sys.executable, "-m", "agentree", "--json"], capture_output=True, text=True, env=jenv,
                             timeout=60)
        found = None
        try:
            data = json.loads(out.stdout)
            for sn in data["sessions"]:
                for wn in sn["worktrees"]:
                    for ag in wn["agents"]:
                        if ag.get("session_id") == sid:
                            found = (sn["session"]["name"], wn["worktree"]["path"], ag)
        except (ValueError, KeyError, TypeError) as e:
            found = None
            print(f"      (--json unreadable: {e}; stderr {out.stderr[-300:]})")
        ag = found[2] if found else {}
        check("agentree --json: provider=claude agent with our session id in session a › the spawned worktree",
              found is not None and found[0] == A and os.path.realpath(found[1]) == wt
              and ag.get("provider") == "claude" and ag.get("kind") == "claude" and ag.get("cc_session_id") == sid
              and ag.get("pid") == pid and ag.get("port") is None, f"{found and found[:2]} {ag}")
        check("agentree --json: status busy from hooks",
              ag.get("status") == "busy" and ag.get("status_source") == "hooks",
              f"{ag.get('status')} / {ag.get('status_source')}")

        # ---- 4. `o` with provider claude (new_agent API): a second claude pane in the worktree's tab,
        #      new session id
        os.unlink(argv_rec)
        snap = snapshot()
        wrow = None
        for n in snap.sessions:
            for wn in n.worktrees:
                if wn.worktree.path == wt:
                    wrow = Row(key=f"w:{n.session.name}:{wt}", kind="worktree", depth=1, caret=None, icon=None,
                               label=(), meta=(), ref=wn, session=n.session.name)
        res = actions().new_agent(wrow, snap, provider="claude") if wrow else None
        check("o (claude): result ok", res is not None and res.ok, res.message if res else "no worktree row")
        argv2 = wait(lambda: read_argv(argv_rec), 8)
        h.track_substitutes(fake)
        check("o (claude): claude got --settings + a fresh --session-id, no prompt",
              bool(argv2) and argv2[1:4] == ["--settings", cc.settings_path(), "--session-id"]
              and len(argv2) == 5 and argv2[4] != sid, f"{argv2!r}")
        tab = next((t for t in tabs(A) if t["name"] == slug), None)
        cl = [p for p in panes(A) if tab and p["tab_id"] == tab["tab_id"] and p.get("title") == "claude"]
        check("o (claude): pane named claude in the worktree's tab", len(cl) >= 1,
              f"{[(p['id'], p.get('title')) for p in panes(A)]}")
        kitty("focus-window", "--match", f"id:{start}")
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
