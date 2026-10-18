"""Remove (`x`): worktree, agent or session — always behind a Confirm, re-validated at execute.

Worktree   refuse the root checkout; refuse if a tab to close holds agentree's own pane; dirty ->
           Confirm.require_text = branch name and `git worktree remove --force`; close every tab
           whose panes are *all* inside the worktree (tabs mixing panes elsewhere are left open,
           with a warning; each close re-checks the session's socket dir + incarnation); SIGTERM
           the agents in it (identity-verified); delete the branch with
           `git update-ref -d refs/heads/<b> <approved tip>` when that tip is merged into the
           default ref (re-checked); drop its links.json record. The plan records the canonical
           path, current branch, HEAD, branch tip and default ref + sha; execute refuses if any
           of them changed. Held under the repo's creation lock.
Agent      SIGTERM after identity verification (start time recorded at plan time, argv still the
           agent's provider's — provider.matches_argv —, ZELLIJ_PANE_ID / ZELLIJ_SESSION_NAME still
           the snapshot's). Records with no provider are opencode (compat); an unknown provider is
           refused.
Session    live -> `zellij kill-session`, exited -> `zellij delete-session`; never the session agentree
           runs in; refused if the name is ambiguous across socket dirs or the server restarted.
Signals go through the runner as `kill -TERM <pid>` (logged, dry-run honoured).
"""
from __future__ import annotations

import os
import shlex
import threading
from typing import Any, Callable

from agentree.actions.api import ActionResult, Confirm, Plan, ProgressEvent
from agentree.actions.jump import Navigator, _Fail, deepest, known_worktrees
from agentree.actions.runner import Runner, canonical_dir, proc_start_time
from agentree.types import Agent, Row, SessionNode, Snapshot, Worktree, WorktreeNode, ZSession


# --------------------------------------------------------------------------- process identity

def _default_identity(pid: int) -> dict | None:
    """{start, argv, env, cwd} of a live process, or None."""
    from agentree.sources import proc

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    ae = proc.proc_args_env(pid)
    if not ae:
        return None
    cwd = proc.proc_cwd([pid], timeout=1.0).get(pid)
    return {"start": proc_start_time(pid), "argv": ae[0], "env": ae[1], "cwd": cwd}


LEGACY_PROVIDER = "opencode"        # records / plans written before providers existed


def _provider_id(a) -> Any:
    """The provider id of an Agent or an agent record (legacy records: "kind", or nothing ->
    opencode). Not validated here (see _provider)."""
    if isinstance(a, dict):
        p = a["provider"] if "provider" in a else a.get("kind")
    else:
        p = getattr(a, "provider", None)
    return LEGACY_PROVIDER if p is None else p


def _provider(pid: Any):
    """The registered provider `pid`, or None if unknown (callers refuse: fail closed)."""
    from agentree.providers import registry

    if not isinstance(pid, str):
        return None
    try:
        return registry.get(pid)
    except KeyError:
        return None


def _is_agent(argv, provider) -> bool:
    return bool(provider.matches_argv(tuple(argv or ())))


UNKNOWN_IDENTITY = "agent identity unknown in this snapshot — refresh (R) and retry"


def _same_dir(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and os.path.realpath(a) == os.path.realpath(b)  # type: ignore[arg-type]


def agent_record(a: Agent, identity: Callable[[int], dict | None]) -> dict | None:
    """Identity to verify at execute time, pinned to what the *snapshot* saw; None if the process
    is gone or already differs. Raises _Fail when it cannot be pinned:

    * start time: the snapshot's `Agent.start_time` is authoritative. If the snapshot has none
      but the live process has one, the live value is NOT adopted (it may be a reused pid):
      refuse. Both known -> must match. Both unknown -> argv/pane/session/cwd checks only.
    * orphan agents (no pane, no session) carry nothing else to pin them by: the live cwd must
      equal the snapshot cwd and argv must be the agent provider's (matches_argv), here and at
      execute time.
    * an agent of an unknown provider is refused (never reinterpreted as another provider).
    """
    pid = _provider_id(a)
    prov = _provider(pid)
    if prov is None:
        raise _Fail(f"agent {a.pid}: unknown agent provider {pid!r} — refusing")
    ident = identity(a.pid)
    if ident is None:
        return None
    live = ident.get("start")
    if a.start_time is None:
        if live is not None:
            raise _Fail(UNKNOWN_IDENTITY)
    elif live is None:
        raise _Fail(UNKNOWN_IDENTITY)
    elif abs(live - a.start_time) > 0.001:
        return None                     # the pid already belongs to another process
    if not _is_agent(ident.get("argv"), prov):
        return None
    orphan = a.pane_id is None and not a.session_name
    if orphan and not _same_dir(ident.get("cwd"), a.cwd):
        raise _Fail(f"agent {a.pid} now runs in {ident.get('cwd') or '?'}, not {a.cwd or '?'} — refresh (R)")
    return {"pid": a.pid, "start": a.start_time, "pane_id": a.pane_id, "session": a.session_name,
            "cwd": a.cwd, "orphan": orphan, "provider": pid}


def verify_agent(rec: dict, identity: Callable[[int], dict | None], inside: str | None = None) -> str | None:
    """None if `rec` still identifies the same agent process (argv matched by the record's
    provider; no provider = opencode), else the reason. An unknown provider is refused."""
    pid = _provider_id(rec)
    prov = _provider(pid)
    if prov is None:
        return f"has an unknown agent provider {pid!r}"
    ident = identity(rec["pid"])
    if ident is None:
        return "has exited"
    if rec.get("start") is not None or ident.get("start") is not None:
        if rec.get("start") is None or ident.get("start") is None:
            return "cannot confirm it is the same process"
        if abs(ident["start"] - rec["start"]) > 0.001:
            return "pid now belongs to another process"
    # start times unknown on both sides: the argv / pane / session / cwd checks below decide
    if not _is_agent(ident.get("argv"), prov):
        return f"is no longer {pid}"
    env = ident.get("env") or {}
    if rec.get("pane_id") is not None and env.get("ZELLIJ_PANE_ID") != str(rec["pane_id"]):
        return "runs in another pane"
    if rec.get("session") and env.get("ZELLIJ_SESSION_NAME") != rec["session"]:
        return "runs in another session"
    if rec.get("orphan") and not _same_dir(ident.get("cwd"), rec.get("cwd")):
        return "runs in another directory"
    if inside is not None:
        cwd = ident.get("cwd")
        if not cwd or not (os.path.realpath(cwd) == inside or os.path.realpath(cwd).startswith(inside + "/")):
            return "is no longer inside the worktree"
    return None


# --------------------------------------------------------------------------- plan

def _all_agents(snap: Snapshot) -> list[tuple[Agent, str | None]]:
    out: list[tuple[Agent, str | None]] = []
    for sn in snap.sessions:
        for wn in sn.worktrees:
            out += [(a, sn.session.name) for a in wn.agents]
        out += [(a, sn.session.name) for a in sn.loose_agents]
    out += [(a, None) for a in snap.orphans.agents]
    return out


def plan_remove(row: Row, snap: Snapshot, *, nav: Navigator, identity=None) -> Plan | ActionResult:
    identity = identity or _default_identity
    try:
        if row.kind == "worktree" and isinstance(row.ref, WorktreeNode):
            return _plan_worktree(row.ref.worktree, snap, nav, identity)
        if row.kind == "agent" and isinstance(row.ref, Agent):
            return _plan_agent(row.ref, row.session, identity)
        if row.kind == "session" and isinstance(row.ref, SessionNode):
            return _plan_session(row.ref, nav, snap.taken_at)
    except _Fail as e:
        return ActionResult(False, str(e))
    return ActionResult(False, "nothing to remove")


def _dirty_count(run: Runner, path: str) -> int | None:
    r = run.query(["git", "-C", path, "status", "--porcelain", "--untracked-files=normal"], timeout=10.0)
    return len([ln for ln in r.stdout.splitlines() if ln.strip()]) if r.ok else None


def _git1(run: Runner, *args: str) -> str | None:
    r = run.query(["git", *args])
    return r.stdout.strip() if r.ok and r.stdout.strip() else None


def worktree_identity(run: Runner, path: str, repo: str, default: str | None) -> dict:
    """What the user approves: canonical path, current branch, HEAD, branch tip, default ref+sha."""
    cpath = os.path.realpath(path)
    ref = _git1(run, "-C", cpath, "symbolic-ref", "-q", "HEAD")
    branch = ref[len("refs/heads/"):] if ref and ref.startswith("refs/heads/") else None
    head = _git1(run, "-C", cpath, "rev-parse", "HEAD")
    tip = _git1(run, "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}") if branch else None
    dsha = _git1(run, "-C", repo, "rev-parse", "--verify", "--quiet", f"{default}^{{commit}}") if default else None
    return {"path": cpath, "branch": branch, "head": head, "tip": tip, "default": default, "default_sha": dsha}


def _plan_worktree(wt: Worktree, snap: Snapshot, nav: Navigator, identity) -> Plan | ActionResult:
    from agentree.actions.spawn import default_ref

    if wt.is_root:
        return ActionResult(False, "refusing to remove the root checkout")
    if not os.path.isdir(wt.path):
        return ActionResult(False, f"{wt.path} is missing (prunable) — run `git worktree prune` in the repo")
    run = nav.runner
    repo = wt.repo_root
    ident = worktree_identity(run, wt.path, repo, default_ref(repo))
    path = ident["path"]
    if ident["head"] is None:
        return ActionResult(False, f"git cannot read {path} — refusing")
    label = ident["branch"] or os.path.basename(path)
    dirty = _dirty_count(run, path)
    if dirty is None:
        return ActionResult(False, f"git status failed in {path} — refusing")
    roots = known_worktrees(snap) | {path, wt.path}
    steps: list[tuple[str, ...]] = []
    spec: list[dict] = []
    warnings: list[str] = []
    lines: list[str] = []

    def inside(cwd: str | None) -> bool:
        return deepest(cwd, roots) in (path, wt.path)

    # tabs: all panes inside this worktree -> close; mixed -> leave open; never agentree's own tab
    for sn in snap.sessions:
        s = sn.session
        if s.state == "exited":
            continue
        own = nav.env.own_tab(s.name, s.socket_dir, [p for t in s.tabs for p in t.panes])
        for t in s.tabs:
            ps = [p for p in t.panes if not p.is_plugin and not p.exited]
            ins = [p for p in ps if inside(p.cwd)]
            if not ins:
                continue
            if len(ins) == len(ps) or t.tab_id == own and len(ins) == len(ps) - 1:
                if t.tab_id == own:
                    return ActionResult(False, "agentree is running in a tab of this worktree; open agentree from another "
                                               "tab (Alt w) to remove it")
                argv, _env = nav.zj(s, "close-tab-by-id", str(t.tab_id))
                steps.append(tuple(argv))
                spec.append({"kind": "close_tab", "session": s.name, "socket_dir": s.socket_dir,
                             "age": s.created_age_s, "tab_id": t.tab_id, "tab": t.name, "argv": argv})
            else:
                warnings.append(f"tab {t.name} in {s.name} also has panes elsewhere: left open")
    # agents inside
    for a, sname in _all_agents(snap):
        if not inside(a.cwd):
            continue
        rec = agent_record(a, identity)
        if rec is None:
            warnings.append(f"agent {a.pid} has already exited")
            continue
        argv = ["kill", "-TERM", str(a.pid)]
        steps.append(tuple(argv))
        spec.append({"kind": "kill_agent", "agent": rec, "argv": argv, "inside": path})
    argv = ["git", "-C", repo, "worktree", "remove", *(["--force"] if dirty else []), path]
    steps.append(tuple(argv))
    spec.append({"kind": "worktree_remove", "argv": argv})
    b, tip, dref, dsha = ident["branch"], ident["tip"], ident["default"], ident["default_sha"]
    if b and tip and dsha:
        if run.query(["git", "-C", repo, "merge-base", "--is-ancestor", tip, dsha]).ok:
            argv = ["git", "-C", repo, "update-ref", "-d", f"refs/heads/{b}", tip]
            steps.append(tuple(argv))
            spec.append({"kind": "branch_delete", "argv": argv})
            lines.append(f"branch {b} ({tip[:10]}) is merged into {dref} ({dsha[:10]}); it will be deleted:")
            lines.append("$ " + shlex.join(argv))
        else:
            lines.append(f"branch {b} is not merged into {dref}; it is kept")
    steps.append(("drop its links.json record",))
    spec.append({"kind": "unlink"})

    title = f"Remove worktree {label}"
    if dirty:
        title += f" — DIRTY: {dirty} changed file{'s' if dirty != 1 else ''}"
        lines.insert(0, f"uncommitted changes will be lost; type {label} to confirm")
    shown = ["$ " + shlex.join(s) if len(s) > 1 else s[0] for s in steps]
    confirm = Confirm(title, tuple(lines + shown + ["! " + w for w in warnings]),
                      require_text=label if dirty else None, danger=True)
    return Plan(kind="remove-worktree", confirm=confirm, steps=tuple(steps), warnings=tuple(warnings),
                data={"spec": spec, "path": path, "repo": repo, "dirty": dirty, "label": label,
                      "roots": sorted(roots), "identity": ident, "taken_at": snap.taken_at})


def _plan_agent(a: Agent, session: str | None, identity) -> Plan | ActionResult:
    rec = agent_record(a, identity)
    if rec is None:
        return ActionResult(False, f"agent {a.pid} has already exited (or its pid now runs something else)")
    if rec.get("session") is None:
        rec["session"] = session if a.pane_id is not None else None
    argv = ["kill", "-TERM", str(a.pid)]
    where = f"pane terminal_{a.pane_id} of {rec['session']}" if a.pane_id is not None else "no zellij pane"
    lines = (f"$ {shlex.join(argv)}", f"agent {a.title or ''} in {a.cwd or '?'} ({where})".replace("  ", " "))
    return Plan(kind="kill-agent", confirm=Confirm(f"Stop agent {a.title or a.pid}", lines, danger=False),
                steps=(tuple(argv),), data={"spec": [{"kind": "kill_agent", "agent": rec, "argv": argv}]})


def _plan_session(node: SessionNode, nav: Navigator, taken_at: float | None) -> Plan | ActionResult:
    s = node.session
    fresh = nav.fresh_session(s.name)
    if fresh.live_dir is not None:
        if nav.env.is_current(s.name, fresh.live_dir):
            return ActionResult(False, "refusing to kill the session agentree runs in")
        argv = [nav.zellij, "kill-session", s.name]
        env = {"ZELLIJ_SOCKET_DIR": fresh.live_dir}
        title = f"Kill session {s.name}"
    else:
        argv = [nav.zellij, "delete-session", s.name]
        env = {"ZELLIJ_SOCKET_DIR": nav.env.exited_socket_dir()}
        title = f"Delete exited session {s.name}"
    lines = [f"$ ZELLIJ_SOCKET_DIR={shlex.quote(env['ZELLIJ_SOCKET_DIR'])} {shlex.join(argv)}"]
    if fresh.live_dir is not None:
        lines += [f"tab {t.name}: {len([p for p in t.panes if not p.is_plugin])} pane(s) will die" for t in s.tabs]
        agents = [a for wn in node.worktrees for a in wn.agents] + list(node.loose_agents)
        lines += [f"agent {a.title or a.pid} (pid {a.pid}) will die" for a in agents]
    return Plan(kind="kill-session", confirm=Confirm(title, tuple(lines), danger=True), steps=(tuple(argv),),
                data={"spec": [{"kind": "session", "name": s.name, "live": fresh.live_dir is not None,
                                "socket_dir": fresh.live_dir, "argv": argv, "env": env,
                                "age": s.created_age_s if s.state != "exited" else None}],
                      "taken_at": taken_at})


# --------------------------------------------------------------------------- execute

def execute_remove(plan: Plan, typed: str | None, nav: Navigator, *, identity=None,
                   progress: Callable[[ProgressEvent], None] | None = None,
                   cancel: threading.Event | None = None, links=None, state_dir: str | None = None
                   ) -> ActionResult:
    if plan.kind == "remove-worktree" and state_dir:
        from agentree.actions.tabs import named_lock, repo_lock_path

        try:
            with named_lock(repo_lock_path(state_dir, plan.data["repo"]),
                            busy="another agentree is changing this repo — try again"):
                return _execute_remove(plan, typed, nav, identity=identity, progress=progress, cancel=cancel,
                                       links=links)
        except _Fail as e:
            return ActionResult(False, str(e))
    return _execute_remove(plan, typed, nav, identity=identity, progress=progress, cancel=cancel, links=links)


def _execute_remove(plan: Plan, typed: str | None, nav: Navigator, *, identity=None,
                    progress: Callable[[ProgressEvent], None] | None = None,
                    cancel: threading.Event | None = None, links=None) -> ActionResult:
    identity = identity or _default_identity
    if plan.errors:
        return ActionResult(False, plan.errors[0])
    c = plan.confirm
    if c is not None and c.require_text is not None and typed != c.require_text:
        return ActionResult(False, "typed text does not match — nothing removed")
    if links is None:
        from agentree import state as links  # type: ignore[no-redef]
    d = plan.data
    spec: list[dict] = d["spec"]
    run = nav.runner
    total = len(spec)
    labels = [shlex.join(s) if len(s) > 1 else s[0] for s in plan.steps]
    notes: list[str] = []
    done = 0

    def ev(i: int, state: str, detail: str = "") -> None:
        if progress is not None:
            try:
                progress(ProgressEvent(i, total, labels[i], state, detail))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass

    if plan.kind == "remove-worktree":
        from agentree.actions.spawn import default_ref

        approved = d["identity"]
        now_ident = worktree_identity(run, d["path"], d["repo"], default_ref(d["repo"])) \
            if os.path.isdir(d["path"]) else None
        if now_ident != approved:
            changed = [k for k in approved if now_ident is None or now_ident.get(k) != approved[k]]
            return ActionResult(False, f"{', '.join(changed) or 'worktree'} changed since confirmation — "
                                       f"nothing removed; re-run x")
        if c is not None and c.require_text is not None and approved["branch"] is not None \
                and typed != approved["branch"]:
            return ActionResult(False, "the branch no longer matches what you typed — nothing removed")
        now = _dirty_count(run, d["path"])
        if now is None:
            return ActionResult(False, "git status failed — nothing removed")
        if now and not d["dirty"]:
            return ActionResult(False, "the worktree has new changes since you confirmed — nothing removed")
    roots = frozenset(d.get("roots") or ())
    for i, st in enumerate(spec):
        if cancel is not None and cancel.is_set():
            for j in range(i, total):
                ev(j, "skipped", "cancelled")
            return ActionResult(False, f"cancelled after {done} step(s)", refresh=done > 0)
        ev(i, "running")
        kind = st["kind"]
        try:
            if kind == "close_tab":
                detail = _close_tab(nav, st, d["path"], roots, d.get("taken_at"))
            elif kind == "kill_agent":
                why = verify_agent(st["agent"], identity, st.get("inside"))
                if why:
                    detail = f"skipped: agent {st['agent']['pid']} {why}"
                    if plan.kind == "kill-agent":
                        ev(i, "failed", detail)
                        return ActionResult(False, f"not stopping agent {st['agent']['pid']}: {why}")
                    notes.append(detail)
                    ev(i, "skipped", detail)
                    continue
                r = run.act(st["argv"])
                if not r.ok:
                    raise _Fail(f"kill: {(r.stderr or '').strip()[:120] or r.code}")
                detail = ""
            elif kind == "worktree_remove":
                r = run.act(st["argv"], timeout=60.0)
                if not r.ok:
                    raise _Fail(f"git worktree remove: {(r.stderr or r.stdout).strip()[:200] or r.code}")
                detail = ""
            elif kind == "branch_delete":
                ident = d["identity"]
                if not run.query(["git", "-C", d["repo"], "merge-base", "--is-ancestor", ident["tip"],
                                  ident["default_sha"]]).ok:
                    notes.append(f"branch kept: {ident['tip'][:10]} is no longer merged into {ident['default']}")
                    ev(i, "skipped", notes[-1])
                    continue
                r = run.act(st["argv"])
                if not r.ok:
                    notes.append(f"branch kept: {(r.stderr or '').strip()[:120]}")
                    ev(i, "skipped", notes[-1])
                    continue
                detail = ""
            elif kind == "unlink":
                run.internal(f"links.json: drop {d['path']}", lambda: links.remove_link(d["path"]))
                detail = ""
            elif kind == "session":
                detail = _kill_session(nav, st, d)
            else:
                raise _Fail(f"unknown step {kind}")
        except _Fail as e:
            ev(i, "failed", str(e))
            for j in range(i + 1, total):
                ev(j, "skipped")
            return ActionResult(False, str(e), refresh=done > 0)
        done += 1
        ev(i, "skipped" if detail.startswith("skipped") else "ok", detail)
    if run.dry_run:
        n = len(run.acted)
        return ActionResult(True, f"dry-run: {n} command{'s' if n != 1 else ''} logged")
    what = {"remove-worktree": f"removed {d.get('label', 'worktree')}",
            "kill-agent": "agent stopped", "kill-session": "session killed"}.get(plan.kind, "done")
    if plan.kind == "kill-session" and not spec[0]["live"]:
        what = "session deleted"
    return ActionResult(True, what + (f" ({'; '.join(notes)})" if notes else ""), refresh=True)


def _close_tab(nav: Navigator, st: dict, path: str, roots: frozenset, taken_at: float | None) -> str:
    """Close the tab only if it is still the same session incarnation (socket dir + created age),
    the tab exists, every pane in it is still inside the worktree, and it is not agentree's own tab.
    Any change of session identity refuses the whole remove (never follows the name)."""
    f = nav.lookup_session(st["session"])
    if f is None or f.live_dir is None:
        return f"skipped: session {st['session']} is gone"
    if st.get("socket_dir") and canonical_dir(f.live_dir) != canonical_dir(st["socket_dir"]):
        raise _Fail(f"session {st['session']} now lives in {f.live_dir} — changed since confirmation; re-run x")
    s = ZSession(st["session"], "attached", st.get("age"), f.live_dir, 1)
    nav.check_incarnation(s, f, taken_at)
    tabs, panes = nav.fresh_inventory(s)
    if not any(t.tab_id == st["tab_id"] for t in tabs):
        return f"skipped: tab {st['tab']} is already gone"
    if nav.env.own_tab(st["session"], f.live_dir, panes) == st["tab_id"]:
        raise _Fail("agentree is running in a tab of this worktree; open agentree from another tab (Alt w) to remove it")
    ps = [p for p in panes if p.tab_id == st["tab_id"] and not p.is_plugin and not p.exited]
    if not ps or any(deepest(p.cwd, roots | {path}) != path for p in ps):
        return f"skipped: tab {st['tab']} now has panes outside the worktree"
    r = nav.runner.act(*nav.zj(s, "close-tab-by-id", str(st["tab_id"])))
    if not r.ok:
        raise _Fail(f"close-tab-by-id {st['tab_id']}: {(r.stderr or '').strip()[:120] or r.code}")
    return ""


def _kill_session(nav: Navigator, st: dict, d: dict) -> str:
    name = st["name"]
    f = nav.fresh_session(name)
    if st["live"]:
        if f.live_dir is None:
            return "skipped: already exited"
        s = ZSession(name, "attached", st.get("age"), st["socket_dir"], 1)
        nav.check_incarnation(s, f, d.get("taken_at"))
        if nav.env.is_current(name, f.live_dir):
            raise _Fail("refusing to kill the session agentree runs in")
        env = {"ZELLIJ_SOCKET_DIR": f.live_dir}
    else:
        if f.live_dir is not None:
            raise _Fail(f"{name} is live again — refresh (R)")
        env = st["env"]
    r = nav.runner.act(st["argv"], env)
    if not r.ok:
        raise _Fail(f"{st['argv'][1]}: {(r.stderr or r.stdout).strip()[:160] or r.code}")
    return ""
