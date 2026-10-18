"""zellij source: sessions across socket dirs, tabs, panes, clients."""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
import time
from typing import NamedTuple

from agentree.actions import exec as ex
from agentree.actions.runner import xdg_socket_dir
from agentree.sources import _par
from agentree.types import SourceWarning, ZPane, ZSession, ZTab

SOURCE = "zellij"


class ParsedSession(NamedTuple):
    name: str
    created_age_s: int | None
    exited: bool
    is_current: bool


# --------------------------------------------------------------------------- socket dirs

def _norm_dir(d: str) -> str:
    """Collapse '//' and trailing slashes but keep the user-facing spelling (/tmp, not /private/tmp):
    the value is passed back to zellij as ZELLIJ_SOCKET_DIR and short socket paths matter."""
    return os.path.normpath(os.path.expanduser(d.strip()))


def socket_dirs(cfg_dirs: list[str] | None = None) -> list[str]:
    """Configured dirs (order kept) else the existing default candidates.
    Deduped by realpath (/tmp -> /private/tmp, $TMPDIR trailing '/')."""
    if cfg_dirs:
        cands = list(cfg_dirs)
        must_exist = False
    else:
        uid = os.getuid()
        tmpdir = os.environ.get("TMPDIR") or "/tmp"
        cands = [os.environ.get("ZELLIJ_SOCKET_DIR") or "", xdg_socket_dir() or "", f"/tmp/zellij-{uid}",
                 os.path.join(tmpdir, f"zellij-{uid}")]
        must_exist = True
    out: list[str] = []
    seen: set[str] = set()
    for c in cands:
        if not c:
            continue
        n = _norm_dir(c)
        if must_exist and not os.path.isdir(n):
            continue
        key = os.path.realpath(n)
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


# --------------------------------------------------------------------------- parsers

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_LINE = re.compile(r"^(?P<name>\S+)\s*(?:\[Created (?P<age>[^\]]*?)\s*ago\])?\s*(?P<rest>.*)$")
_AGE_PART = re.compile(r"(\d+)\s*(weeks?|w|days?|d|hours?|h|minutes?|mins?|m|seconds?|secs?|s)\b")
_UNIT_S = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_age(text: str) -> int | None:
    total = 0
    found = False
    for num, unit in _AGE_PART.findall(text):
        u = unit.lower()
        if u.startswith("w"):
            k = "w"
        elif u.startswith("d"):
            k = "d"
        elif u.startswith("h"):
            k = "h"
        elif u.startswith("m"):
            k = "m"
        else:
            k = "s"
        total += int(num) * _UNIT_S[k]
        found = True
    return total if found else None


def parse_list_sessions(text: str) -> list[ParsedSession]:
    out: list[ParsedSession] = []
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line or line.startswith("No active zellij sessions"):
            continue
        m = _SESSION_LINE.match(line)
        if not m:
            continue
        rest = m.group("rest") or ""
        age = parse_age(m.group("age")) if m.group("age") is not None else None
        out.append(ParsedSession(m.group("name"), age, "EXITED" in rest, "(current)" in rest))
    return out


def _load_json_list(text: str) -> list:
    data = json.loads(text) if text.strip() else []
    if not isinstance(data, list):
        raise ValueError("expected a JSON list")
    return data


def parse_list_tabs(json_text: str) -> list[ZTab]:
    tabs = []
    for t in _load_json_list(json_text):
        tabs.append(ZTab(
            tab_id=int(t.get("tab_id", t.get("position", 0))),
            position=int(t.get("position", 0)),
            name=str(t.get("name", "")),
            active=bool(t.get("active", False)),
        ))
    tabs.sort(key=lambda t: t.position)
    return tabs


def _split_cmd(cmd: str | None) -> tuple[str, ...]:
    if not cmd:
        return ()
    try:
        return tuple(shlex.split(cmd))
    except ValueError:
        return tuple(cmd.split())


def _norm_cwd(p: str | None) -> str | None:
    if not p:
        return None
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return p


def parse_list_panes(json_text: str) -> list[ZPane]:
    """`list-panes --tab --command --state --json`. Real fields: id, is_plugin, title,
    terminal_command (null for the default shell), pane_command (always the running cmd for
    terminals), pane_cwd, tab_id, is_focused, is_floating, exited, is_selectable."""
    panes = []
    for p in _load_json_list(json_text):
        is_plugin = bool(p.get("is_plugin", False))
        cmd_str = None if is_plugin else (p.get("terminal_command") or p.get("pane_command"))
        argv = _split_cmd(cmd_str)
        command = os.path.basename(argv[0]) if argv else None
        panes.append(ZPane(
            pane_id=int(p.get("id", 0)),
            is_plugin=is_plugin,
            tab_id=int(p.get("tab_id", 0)),
            title=str(p.get("title") or ""),
            command=command,
            argv=argv,
            cwd=_norm_cwd(p.get("pane_cwd")),
            focused=bool(p.get("is_focused", False)),
            floating=bool(p.get("is_floating", False)),
            exited=bool(p.get("exited", False)),
            selectable=bool(p.get("is_selectable", True)),
        ))
    return panes


def parse_list_clients(text: str) -> int:
    """`zellij action list-clients` prints a header `CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND`
    followed by one row per client (first column numeric)."""
    n = 0
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].isdigit():
            n += 1
    return n


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.realpath(_norm_dir(a)) == os.path.realpath(_norm_dir(b))


def merge_sessions(per_dir: dict[str, list[ParsedSession]], prefer_dir: str | None = None
                   ) -> list[tuple[ParsedSession, str | None]]:
    """Merge listings from several socket dirs. A live entry in any dir wins over EXITED
    (the resurrection cache is shared, so every dir lists every exited session). A name live in
    several dirs (see `live_duplicates`) keeps the entry from `prefer_dir` (the caller's
    $ZELLIJ_SOCKET_DIR) if live there, else the first dir in `per_dir` order (configured order).
    Returns [(parsed, live_socket_dir | None)] in first-seen order."""
    order: list[str] = []
    best: dict[str, tuple[ParsedSession, str | None]] = {}
    for d, items in per_dir.items():
        for s in items:
            if s.name not in best:
                order.append(s.name)
                best[s.name] = (s, None if s.exited else d)
                continue
            cur, cur_d = best[s.name]
            if cur.exited and not s.exited:
                best[s.name] = (s, d)
            elif cur.exited and s.exited:
                # keep the youngest age for exited duplicates (most recent incarnation)
                if s.created_age_s is not None and (cur.created_age_s is None or s.created_age_s < cur.created_age_s):
                    best[s.name] = (s, None)
            elif not cur.exited and not s.exited:
                if _same_dir(d, prefer_dir) and not _same_dir(cur_d, prefer_dir):
                    best[s.name] = (s, d)
    return [best[n] for n in order]


def live_duplicates(per_dir: dict[str, list[ParsedSession]]) -> dict[str, list[str]]:
    """{session name: [socket dirs]} for names live in more than one socket dir. Such a name
    cannot be addressed unambiguously by name, so the session must be treated as incomplete."""
    where: dict[str, list[str]] = {}
    for d, items in per_dir.items():
        for s in items:
            if not s.exited and d not in where.setdefault(s.name, []):
                where[s.name].append(d)
    return {n: ds for n, ds in where.items() if len(ds) > 1}


# --------------------------------------------------------------------------- fetch

def _zellij() -> str:
    return ex.which("zellij") or "zellij"


# Inherited from the zellij pane agentree runs in. With them set, `list-sessions` in a socket dir that
# does NOT host the current session still prints it as live "(current)" (zellij 0.45.1, verified):
# a false "live in two socket dirs". ex.run only overlays env, so they are removed with env -u.
CLIENT_ENV = ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")
ENV_BIN = "/usr/bin/env"


def list_sessions_argv(zj: str) -> list[str]:
    """argv for `zellij list-sessions -n` without the caller's zellij client env (run it with
    env={"ZELLIJ_SOCKET_DIR": <dir>}). Shared by the source and `agentree doctor`."""
    unset = [x for k in CLIENT_ENV for x in ("-u", k)]
    return [ENV_BIN, *unset, zj, "list-sessions", "-n"]


def _list_sessions(zj: str, d: str, timeout: float):
    return ex.run(list_sessions_argv(zj), env={"ZELLIJ_SOCKET_DIR": d}, timeout=timeout)


def has_socket(d: str, name: str) -> bool:
    """Is there a server socket for session `name` in socket dir `d` (<d>/contract_version_*/<name>)?
    An unreadable dir cannot be checked and counts as yes (zellij's listing is trusted)."""
    try:
        entries = os.listdir(d)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    for e in entries:
        if not e.startswith("contract_version_"):
            continue
        try:
            st = os.stat(os.path.join(d, e, name))
        except OSError:
            continue
        if stat.S_ISSOCK(st.st_mode):
            return True
    return False


def verify_live(per_dir: dict[str, list[ParsedSession]], socket_check=None) -> dict[str, list[ParsedSession]]:
    """Downgrade to exited every session listed live in a dir that has no socket for it (zellij can
    report a session live in a dir it does not run in, see CLIENT_ENV)."""
    check = socket_check or has_socket
    return {d: [s if s.exited or check(d, s.name) else s._replace(exited=True, is_current=False) for s in items]
            for d, items in per_dir.items()}


def _session_action(zj: str, d: str, name: str, args: list[str], timeout: float):
    return ex.run([zj, "--session", name, "action", *args], env={"ZELLIJ_SOCKET_DIR": d}, timeout=timeout)


def _err(r) -> str:
    if r.timed_out:
        return "timed out"
    return (r.stderr or r.stdout).strip().splitlines()[0][:200] if (r.stderr or r.stdout).strip() else f"exit {r.code}"


class _Inv(NamedTuple):
    tabs: list[ZTab]
    panes: list[ZPane]
    errors: list[str]           # list-tabs / list-panes failures
    unknown_tabs: list[int]     # tab ids referenced by panes but not listed


def _res(outcome) -> ex.Result | None:
    return _par.value(outcome)


def _parse_inventory(rt: ex.Result | None, rp: ex.Result | None) -> _Inv:
    errors: list[str] = []
    tabs: list[ZTab] = []
    panes: list[ZPane] = []
    try:
        if rt is None:
            raise ValueError("timed out")
        if not rt.ok:
            raise ValueError(_err(rt))
        tabs = parse_list_tabs(rt.stdout)
    except ValueError as e:
        errors.append(f"list-tabs failed: {e}")
    try:
        if rp is None:
            raise ValueError("timed out")
        if not rp.ok:
            raise ValueError(_err(rp))
        panes = parse_list_panes(rp.stdout)
    except ValueError as e:
        errors.append(f"list-panes failed: {e}")
    unknown: list[int] = []
    if not errors:
        known = {t.tab_id for t in tabs}
        unknown = sorted({p.tab_id for p in panes} - known)
    return _Inv(tabs, panes, errors, unknown)


parse_inventory = _parse_inventory   # public alias (actions re-validate a session's inventory)

TABS_ARGS = ["list-tabs", "--all", "--json"]
PANES_ARGS = ["list-panes", "--tab", "--command", "--state", "--json"]
CLIENTS_ARGS = ["list-clients"]


def fetch(cfg_socket_dirs: list[str] | None = None, timeout: float = 1.0,
          current_session: str | None = None) -> tuple[tuple[ZSession, ...], list[SourceWarning]]:
    """All sessions across socket dirs with tabs/panes/clients for live ones.

    Three timed phases at most (each <= timeout): list-sessions per dir; tabs+panes+clients per
    live session; one retry of tabs+panes for sessions whose listing failed or was inconsistent
    (panes in unlisted tabs, e.g. a tab opened between the two calls). A session still
    inconsistent after the retry, or live in two socket dirs, gets inventory_complete=False.
    """
    if current_session is None:
        current_session = os.environ.get("ZELLIJ_SESSION_NAME")
    warnings: list[SourceWarning] = []
    zj = _zellij()
    dirs = socket_dirs(cfg_socket_dirs)
    if not dirs:
        return (), [SourceWarning(SOURCE, "no zellij socket dir found")]

    def phase(fns):
        return _par.run_all(fns, workers=16, deadline=time.monotonic() + timeout + 0.5, name="agentree-zellij")

    ls = phase([lambda d=d: _list_sessions(zj, d, timeout) for d in dirs])
    per_dir: dict[str, list[ParsedSession]] = {}
    for d, out in zip(dirs, ls):
        r = _res(out)
        if r is None:
            warnings.append(SourceWarning(SOURCE, f"list-sessions in {d}: timed out"))
            continue
        if r.code == 127:
            return (), [SourceWarning(SOURCE, "zellij binary not found")]
        if r.ok or "No active zellij sessions" in (r.stdout + r.stderr):
            per_dir[d] = parse_list_sessions(r.stdout)
        else:
            warnings.append(SourceWarning(SOURCE, f"list-sessions in {d}: {_err(r)}"))
    per_dir = verify_live(per_dir)
    env_dir = os.environ.get("ZELLIJ_SOCKET_DIR")
    merged = merge_sessions(per_dir, env_dir)
    dupes = live_duplicates(per_dir)
    live = [(s, d) for s, d in merged if d is not None]

    fns = []
    for s, d in live:
        for args in (TABS_ARGS, PANES_ARGS, CLIENTS_ARGS):
            fns.append(lambda d=d, n=s.name, a=args: _session_action(zj, d, n, a, timeout))
    res = phase(fns)
    invs: dict[str, _Inv] = {}
    clients_res: dict[str, ex.Result | None] = {}
    for i, (s, _d) in enumerate(live):
        invs[s.name] = _parse_inventory(_res(res[3 * i]), _res(res[3 * i + 1]))
        clients_res[s.name] = _res(res[3 * i + 2])

    retry = [(s, d) for s, d in live if invs[s.name].errors or invs[s.name].unknown_tabs]
    if retry:
        fns = []
        for s, d in retry:
            for args in (TABS_ARGS, PANES_ARGS):
                fns.append(lambda d=d, n=s.name, a=args: _session_action(zj, d, n, a, timeout))
        res = phase(fns)
        for i, (s, _d) in enumerate(retry):
            invs[s.name] = _parse_inventory(_res(res[2 * i]), _res(res[2 * i + 1]))

    sessions: list[ZSession] = []
    for s, d in merged:
        # our own session: $ZELLIJ_SESSION_NAME, live (socket-verified; a name live in two dirs
        # resolves to $ZELLIJ_SOCKET_DIR via merge_sessions). list-sessions' "(current)" marker
        # is not used: its client env is removed (see CLIENT_ENV)
        is_current = bool(current_session) and s.name == current_session and d is not None
        if d is None:
            sessions.append(ZSession(s.name, "exited", s.created_age_s, None, 0, is_current, (), True))
            continue
        inv = invs[s.name]
        complete = True
        for e in inv.errors:
            complete = False
            warnings.append(SourceWarning(SOURCE, f"{s.name}: {e}"))
        if inv.unknown_tabs:
            complete = False
            warnings.append(SourceWarning(SOURCE, f"{s.name}: panes reference unknown tabs {inv.unknown_tabs} "
                                                  f"(inventory inconsistent after retry)"))
        if s.name in dupes:
            complete = False
            warnings.append(SourceWarning(SOURCE, f"session {s.name} is live in two socket dirs "
                                                  f"({', '.join(dupes[s.name])}); actions disabled"))
        rc = clients_res[s.name]
        clients = parse_list_clients(rc.stdout) if rc is not None and rc.ok else 0
        if rc is None or not rc.ok:
            warnings.append(SourceWarning(SOURCE, f"{s.name}: list-clients failed: {_err(rc) if rc else 'timed out'}"))
        by_tab: dict[int, list[ZPane]] = {}
        for p in inv.panes:
            by_tab.setdefault(p.tab_id, []).append(p)
        tabs_full = tuple(
            ZTab(t.tab_id, t.position, t.name, t.active, tuple(by_tab.get(t.tab_id, ())))
            for t in inv.tabs
        )
        sessions.append(ZSession(
            name=s.name,
            state="attached" if clients > 0 else "detached",
            created_age_s=s.created_age_s,
            socket_dir=d,
            clients=clients,
            is_current=is_current,
            tabs=tabs_full,
            inventory_complete=complete,
        ))
    return tuple(sessions), warnings


TITLES_ARGS = ["list-panes", "--json"]


def fetch_pane_titles(targets, timeout: float = 1.0) -> tuple[dict[tuple[str, int], str], list[str]]:
    """Fresh terminal pane titles for live sessions: targets = [(session name, socket dir)].
    One `list-panes --json` per session, in parallel. Returns ({(session, pane id): title},
    names of sessions whose listing failed)."""
    targets = [(n, d) for n, d in targets if n and d]
    if not targets:
        return {}, []
    zj = _zellij()
    res = _par.run_all([lambda n=n, d=d: _session_action(zj, d, n, TITLES_ARGS, timeout) for n, d in targets],
                       workers=16, deadline=time.monotonic() + timeout + 0.2, name="agentree-zellij-titles")
    out: dict[tuple[str, int], str] = {}
    failed: list[str] = []
    for (n, _d), o in zip(targets, res):
        r = _res(o)
        if r is None or not r.ok:
            failed.append(n)
            continue
        try:
            panes = parse_list_panes(r.stdout)
        except (ValueError, TypeError):
            failed.append(n)
            continue
        for p in panes:
            if not p.is_plugin and p.title:
                out[(n, p.pane_id)] = p.title
    return out, failed
