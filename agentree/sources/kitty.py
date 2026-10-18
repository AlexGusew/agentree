"""kitty source: windows whose foreground process is a zellij client, via `kitty @ ls`."""
from __future__ import annotations

import glob
import json
import os
import re
import stat
import time

from agentree.actions import exec as ex
from agentree.sources import _par
from agentree.types import KittyHost, SourceWarning

SOURCE = "kitty"
SOCKET_GLOB = "/tmp/kitty.sock-*"
KITTY_FALLBACK = "/Applications/kitty.app/Contents/MacOS/kitty"
TITLE_SEP = " | "
USER_VAR = "agentree_session"     # set by agentree on windows it launches (`kitty @ launch --var agentree_session=<S>`)

# zellij flags that take a value (so the next token is not the session name)
_VALUE_FLAGS = {"-c", "--config", "--config-dir", "-l", "--layout", "--data-dir", "--max-panes",
                "--new-session-with-layout", "-n", "--default-layout"}
# flags of `zellij attach` that take a value (`-c` there means --create, a boolean)
_ATTACH_VALUE_FLAGS = {"--index", "--layout", "-l"}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_SOCK_NAME = re.compile(r"kitty\.sock-(\d+)")
_TMP_REAL = os.path.realpath("/tmp")


def is_owned_socket(spec: str | None) -> bool:
    """Owned-socket policy (findings), shared by discovery and by every use of a cached socket:
    a unix socket directly under /tmp (canonically: realpath of its directory is /private/tmp)
    named `kitty.sock-<pid>` with <pid> alive, owned by the current user. Accepts `unix:<path>`."""
    if not spec:
        return False
    path = spec[len("unix:"):] if spec.startswith("unix:") else spec
    if not path.startswith("/"):
        return False
    m = _SOCK_NAME.fullmatch(os.path.basename(path))
    if not m or os.path.realpath(os.path.dirname(path)) != _TMP_REAL:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid() and _pid_alive(int(m.group(1)))


def sockets(pattern: str | None = None) -> list[str]:
    """Owned kitty remote-control sockets matching `pattern` (config `kitty_socket_glob`),
    as `unix:<path>`; whatever the glob, every match must pass `is_owned_socket`."""
    return [f"unix:{p}" for p in sorted(glob.glob(os.path.expanduser(pattern or SOCKET_GLOB)))
            if is_owned_socket(p)]


def same_socket(a: str | None, b: str | None) -> bool:
    """Canonical compare of two socket specs (/tmp vs /private/tmp, unix: prefix)."""
    def canon(x: str) -> str:
        x = x[len("unix:"):] if x.startswith("unix:") else x
        return os.path.join(os.path.realpath(os.path.dirname(x)), os.path.basename(x))
    return bool(a) and bool(b) and canon(a) == canon(b)  # type: ignore[arg-type]


def session_from_argv(argv: list[str] | tuple[str, ...]) -> tuple[bool, str | None]:
    """(is_zellij_client, session name or None) for a foreground cmdline.

    Recognises `zellij attach|a [flags] <name>`, `zellij -s|--session <name>`, `zellij --session=<name>`.
    A plain `zellij` (or attach without a name) is a client with an unknown session."""
    if not argv or os.path.basename(argv[0]) != "zellij":
        return False, None
    args = list(argv[1:])
    if any(a in ("--server",) for a in args):
        return False, None
    name = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-s", "--session") and i + 1 < len(args):
            name = args[i + 1]
            i += 2
            continue
        if a.startswith("--session="):
            name = a.split("=", 1)[1]
        elif a in ("attach", "a"):
            j = i + 1
            while j < len(args):
                b = args[j]
                if b in _ATTACH_VALUE_FLAGS:
                    j += 2
                    continue
                if b.startswith("-"):
                    j += 1
                    continue
                if b == "options":
                    break
                return True, b
            return True, name
        elif a in _VALUE_FLAGS:
            i += 2
            continue
        elif not a.startswith("-") and a not in ("options",):
            # other subcommands (action, list-sessions, run, ...) are not interactive clients
            if a in ("action", "ac", "list-sessions", "ls", "kill-session", "k", "delete-session", "d",
                     "kill-all-sessions", "ka", "delete-all-sessions", "da", "run", "r", "edit", "e",
                     "setup", "plugin", "pipe", "web", "convert-config", "convert-layout", "convert-theme"):
                return False, None
        i += 1
    return True, name


def title_session(title: str | None) -> str | None:
    if not title or TITLE_SEP not in title:
        return None
    return title.split(TITLE_SEP, 1)[0].strip() or None


def resolve_window(win: dict, sessions: set[str]) -> str | None:
    """Session a kitty window hosts, or None. Findings: corroborate title + argv; when
    title_overridden is set the title is the user's and argv alone may be stale -> reject.

      foreground process is not a zellij client          -> reject
      title_overridden                                    -> reject
      argv names a session: accept only if the title is present and its `<name> | ` prefix
        equals the argv name; any contradiction (or no title) rejects, regardless of inventory
      bare `zellij` (no name in argv): accept by title only if its prefix names a known session
      agentree-launched window (user var agentree_session == argv name): zellij only sets the terminal title
        when a pane title changes, so a freshly attached client keeps kitty's default title.
        Accept when the title names no session at all (no `<x> | ` prefix); a title naming any
        other session is still a contradiction.
    """
    argv_name = None
    is_client = False
    for p in win.get("foreground_processes") or []:
        c, n = session_from_argv(p.get("cmdline") or [])
        if c:
            is_client = True
            if n:
                argv_name = n
                break
    if not is_client or bool(win.get("title_overridden", False)):
        return None
    tname = title_session(win.get("title"))
    if argv_name:
        if tname == argv_name:
            return argv_name
        marker = (win.get("user_vars") or {}).get(USER_VAR)
        if tname is None and marker == argv_name:
            return argv_name
        return None
    return tname if tname and tname in sessions else None


def parse_ls(json_text: str, socket: str, sessions: set[str]) -> list[KittyHost]:
    data = json.loads(json_text) if json_text.strip() else []
    out: list[KittyHost] = []
    for ow in data if isinstance(data, list) else []:
        ow_focused = bool(ow.get("is_focused", False))
        for tab in ow.get("tabs") or []:
            tab_focused = bool(tab.get("is_focused", False))
            for win in tab.get("windows") or []:
                name = resolve_window(win, sessions)
                if not name:
                    continue
                out.append(KittyHost(
                    socket=socket,
                    os_window_id=int(ow.get("id", 0)),
                    tab_id=int(tab.get("id", 0)),
                    window_id=int(win.get("id", 0)),
                    session=name,
                    focused=ow_focused and tab_focused and bool(win.get("is_focused", False)),
                ))
    return out


def kitty_bin() -> str:
    return ex.which("kitty") or KITTY_FALLBACK


def _ls(kitty: str, sock: str, timeout: float):
    return ex.run([kitty, "@", "--to", sock, "ls"], timeout=timeout)


def fetch(sessions: set[str] | list[str] | tuple[str, ...], timeout: float = 1.0,
          socket_glob: str | None = None) -> tuple[tuple[KittyHost, ...], list[SourceWarning]]:
    sset = set(sessions)
    socks = sockets(socket_glob)
    if not socks:
        return (), []
    kitty = kitty_bin()
    hosts: list[KittyHost] = []
    warnings: list[SourceWarning] = []
    results = _par.run_all([lambda s=s: _ls(kitty, s, timeout) for s in socks], workers=8,
                           deadline=time.monotonic() + timeout + 0.5, name="agentree-kitty")
    for sock, out in zip(socks, results):
        r = _par.value(out)
        if r is None:
            warnings.append(SourceWarning(SOURCE, f"{sock}: ls failed: timed out"))
            continue
        if not r.ok:
            msg = "timed out" if r.timed_out else (r.stderr.strip().splitlines() or [f"exit {r.code}"])[0][:200]
            warnings.append(SourceWarning(SOURCE, f"{sock}: ls failed: {msg}"))
            continue
        try:
            hosts.extend(parse_ls(r.stdout, sock, sset))
        except (ValueError, TypeError, AttributeError) as e:
            warnings.append(SourceWarning(SOURCE, f"{sock}: bad ls output: {e}"))
    return tuple(hosts), warnings
