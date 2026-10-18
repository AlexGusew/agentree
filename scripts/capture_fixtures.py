#!/usr/bin/env python3
"""Capture REAL read-only output from this machine into tests/fixtures/live/.

Read-only: zellij list-*/dump-layout, git worktree list/status/for-each-ref/log, kitty @ ls,
lsof, opencode GET endpoints. Long strings are truncated; auth-ish keys and process env are
redacted. Run from the repo root:  python3 scripts/capture_fixtures.py
Set AGENTREE_FIXTURE_REPO to the repo whose git output to capture (default ~/Projects/web-app).
Captured output is real machine data: sanitize names, paths and conversation text before committing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agentree.actions import exec as ex  # noqa: E402
from agentree.providers import opencode  # noqa: E402
from agentree.sources import git, kitty, proc, zellij  # noqa: E402

OUT = os.path.join(ROOT, "tests", "fixtures", "live")
REPO = os.path.expanduser(os.environ.get("AGENTREE_FIXTURE_REPO", "~/Projects/web-app"))
MAX_STR = 400
SECRET_KEY = re.compile(r"(^|_)(token|secret|password|passwd|apikey|api_key|authorization|cookie|credential)s?$|"
                        r"^(key|auth|access|refresh|headers)$", re.I)
ENV_KEEP = re.compile(r"^(ZELLIJ.*|TERM|TERM_PROGRAM|SHELL|KITTY_WINDOW_ID|KITTY_PID)$")

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def scrub(obj, key: str = ""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k != "tokens" and SECRET_KEY.search(str(k)) and not isinstance(v, (int, float, bool)) and v not in (None, {}, []):
                out[k] = "<redacted>"
            elif k == "env" and isinstance(v, dict):
                out[k] = {ek: ev for ek, ev in v.items() if ENV_KEEP.match(ek)}
            else:
                out[k] = scrub(v, k)
        return out
    if isinstance(obj, list):
        return [scrub(x, key) for x in obj]
    if isinstance(obj, str) and len(obj) > MAX_STR:
        return obj[:MAX_STR] + "…<truncated>"
    return obj


def write(name: str, data, mode: str = "w") -> None:
    path = os.path.join(OUT, name)
    if isinstance(data, (dict, list)):
        data = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        mode = "w"
    with open(path, mode + ("b" if isinstance(data, bytes) else ""), **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
        fh.write(data)
    print(f"  wrote {name} ({len(data)} bytes)")


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def capture_zellij() -> list[str]:
    print("zellij")
    zj = ex.which("zellij") or "zellij"
    per_dir = {}
    for d in zellij.socket_dirs(None):
        # without the caller's ZELLIJ* env (inside a session it is reported live in every dir)
        r = ex.run(zellij.list_sessions_argv(zj), env={"ZELLIJ_SOCKET_DIR": d}, timeout=3)
        tag = "tmp" if d.startswith("/tmp") else "tmpdir"
        write(f"zellij_list_sessions_{tag}.txt", r.stdout)
        per_dir[d] = zellij.parse_list_sessions(r.stdout)
    names = []
    for s, d in zellij.merge_sessions(zellij.verify_live(per_dir)):
        if d is None:
            continue
        names.append(s.name)
        env = {"ZELLIJ_SOCKET_DIR": d}
        base = [zj, "--session", s.name, "action"]
        n = slug(s.name)
        write(f"zellij_{n}_list_tabs.json", ex.run(base + ["list-tabs", "--all", "--json"], env=env, timeout=3).stdout)
        write(f"zellij_{n}_list_panes.json",
              ex.run(base + ["list-panes", "--tab", "--command", "--state", "--json"], env=env, timeout=3).stdout)
        write(f"zellij_{n}_list_clients.txt", ex.run(base + ["list-clients"], env=env, timeout=3).stdout)
        write(f"zellij_{n}_dump_layout.kdl", ex.run(base + ["dump-layout"], env=env, timeout=3).stdout)
    return names


def capture_git() -> None:
    print("git")
    if not os.path.isdir(os.path.join(REPO, ".git")):
        print(f"  skip: {REPO} is not a git repo")
        return
    r = ex.run(["git", "--no-optional-locks", "worktree", "list", "--porcelain", "-z"], cwd=REPO, timeout=5)
    write("git_worktree_list_porcelain_z.bin", r.stdout.encode("utf-8"))
    recs = git.parse_worktree_porcelain_z(r.stdout)
    default_ref = git._default_ref(os.path.realpath(REPO), 3)
    branches = [x["branch"] for x in recs if x.get("branch")]
    fr = ex.run(["git", "--no-optional-locks", "for-each-ref", f"--format={git.for_each_ref_format(default_ref)}",
                 *branches], cwd=REPO, timeout=5)
    write("git_for_each_ref.bin", fr.stdout.encode("utf-8"))
    det = [x["HEAD"] for x in recs if not x.get("branch") and x.get("HEAD")]
    if det:
        lg = ex.run(["git", "--no-optional-locks", "log", "--no-walk=unsorted", "--format=%H%x00%ct%x00%s", *det],
                    cwd=REPO, timeout=5)
        write("git_log_no_walk.bin", lg.stdout.encode("utf-8"))
    for rec in recs[:4]:
        p = rec["worktree"]
        st = ex.run(["git", "-c", "core.quotepath=off", "--no-optional-locks", "status", "--porcelain=v2", "--branch"],
                    cwd=p, timeout=5)
        write(f"git_status_v2_{slug(os.path.basename(p))}.txt", st.stdout)


def capture_kitty() -> None:
    print("kitty")
    for sock in kitty.sockets()[:1]:
        r = ex.run([kitty.kitty_bin(), "@", "--to", sock, "ls"], timeout=3)
        if r.ok:
            write("kitty_ls.json", scrub(json.loads(r.stdout)))
            write("kitty_socket.txt", sock + "\n")


def _get(port: int, path: str):
    with _opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as resp:
        return json.loads(resp.read())


def capture_opencode() -> None:
    print("opencode")
    ps = proc.list_procs()
    pids = opencode.find_opencode(ps)
    lsof = ex.run(["lsof", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fpn", "-p", ",".join(map(str, pids))], timeout=3)
    write("lsof_listen.txt", lsof.stdout)
    cwd = ex.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))], timeout=3)
    write("lsof_cwd.txt", cwd.stdout)
    write("ps_opencode.txt", "".join(f"{p} {pp} {c}\n" for p, pp, c in ps if p in pids))
    ports = proc.parse_lsof_listen(lsof.stdout)
    port = None
    for pid in pids:
        ae = proc.proc_args_env(pid)
        port = opencode.port_from_argv(ae[0]) if ae else None
        port = port or (ports.get(pid) or [None])[0]
        if port:
            break
    if not port:
        print("  skip: no opencode port")
        return
    with _opener.open(f"http://127.0.0.1:{port}/doc", timeout=5) as resp:
        write("opencode_openapi.json", resp.read().decode("utf-8"))  # compact, as served
    sessions = _get(port, "/session?roots=true&limit=5")
    write("opencode_session.json", scrub(sessions[:5]))
    write("opencode_session_status.json", scrub(_get(port, "/session/status")))
    write("opencode_permission.json", scrub(_get(port, "/permission")))
    write("opencode_question.json", scrub(_get(port, "/question")))
    if sessions:
        msgs = _get(port, f"/session/{sessions[0]['id']}/message?limit=4")
        write("opencode_messages.json", scrub(msgs))
    prov = _get(port, "/config/providers")
    # keep only the limit-relevant shape
    slim = {"default": prov.get("default"), "providers": [
        {"id": p.get("id"), "models": {mid: {"id": m.get("id"), "limit": m.get("limit")}
                                       for mid, m in (p.get("models") or {}).items()}}
        for p in prov.get("providers") or []]}
    write("opencode_config_providers.json", slim)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    capture_zellij()
    capture_git()
    capture_kitty()
    capture_opencode()


if __name__ == "__main__":
    main()
