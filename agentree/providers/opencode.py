"""opencode provider: running opencode processes + their local HTTP API (127.0.0.1 only).

Agents carry `meta={"port": int | None, "api_ok": bool}`; live status comes from the server-sent
event stream (agentree.providers._opencode_events, imported lazily by the watcher).

API facts (opencode 1.18.27, verified live):
- Every request is scoped to an "instance directory" via `?directory=<abs path>` (defaults to the
  server's cwd). Sessions are persisted per *project* (git root commit), so several opencode
  processes in the same project list the same sessions; `/session/status`, `/permission` and
  `/question` are in-memory and per process+directory.
- GET /session?roots=true&limit=N   -> [Session] newest first (id, title, directory, parentID,
                                        time.created/updated ms)
- GET /session/status               -> {sessionID: {"type": "idle"|"busy"|"retry", ...}}; idle
                                        sessions are usually simply absent
- GET /permission, GET /question    -> pending requests [{id, sessionID, ...}]
- GET /session/{id}/message?limit=N -> last N messages, oldest first: [{info, parts}]
                                        info.tokens {total?, input, output, reasoning, cache{read,write}}
- GET /config/providers             -> {providers: [{id, models: {id: {limit: {context}}}}]}

Because the session list is shared, the session a process is *showing* comes from its zellij pane
title (the TUI sets `OC | <session title>`, possibly truncated, or `OpenCode` on the home screen);
see `select_session`. /session/status is per process (verified: only the server running a turn
reports it busy), so busy sessions there belong to that process.
"""
from __future__ import annotations

import json
import os
import shlex
import threading
import time
import socket
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Mapping, NamedTuple, Sequence

from agentree.providers.base import (DemoAgent, DemoContext, DoctorJob, FetchContext, FetchResult, Launch,
                                LaunchRequest, ProviderSpec, RefreshPolicy, SetupStep, Watcher)
from agentree.sources import _par
from agentree.sources import proc as procmod
from agentree.types import Agent, AgentStatus, SourceWarning, ZPane

if TYPE_CHECKING:
    from agentree.actions.runner import Runner
    from agentree.diagnostics import Check, Probe

SOURCE = "opencode"
UNAVAILABLE_NOTE = "Live status unavailable"
HTTP_TIMEOUT = 0.4
DONE_AFTER_S = 30 * 60
MESSAGE_LIMIT = 10
LAST_MESSAGE_MAX = 600

MAX_RESPONSE = 4 << 20             # bytes; larger responses are a failure
HOST = "127.0.0.1"
_limits_cache: dict[tuple[int, int], dict[tuple[str, str], int]] = {}
_limits_lock = threading.Lock()


# --------------------------------------------------------------------------- process discovery

_NON_SERVER_SUBCOMMANDS = {
    "run", "auth", "models", "upgrade", "uninstall", "stats", "export", "import", "github",
    "pr", "debug", "mcp", "agent", "completion", "acp", "session", "db", "plugin", "providers",
}


def _is_opencode_name(path: str) -> bool:
    base = os.path.basename(path)
    return base in ("opencode", ".opencode", "opencode-auto") or base.startswith(".opencode-")


def is_opencode_argv(argv: tuple[str, ...] | list[str] | Sequence[str]) -> bool:
    if not argv:
        return False
    if not _is_opencode_name(argv[0]):
        return False
    if os.path.basename(argv[0]) == "opencode-auto":
        return False  # wrapper script (if it shows up as argv0); the real binary is its child
    # first non-flag positional = subcommand
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        return a not in _NON_SERVER_SUBCOMMANDS
    return True


def find_opencode(procs: Iterable[tuple[int, int, str]] | Iterable[procmod.Proc]) -> list[int]:
    """PIDs of opencode TUI/server processes from `list_procs()` tuples or Proc records."""
    me = {os.getpid()}
    out: list[int] = []
    for p in procs:
        if isinstance(p, procmod.Proc):
            pid, argv = p.pid, p.argv
        else:
            pid, _ppid, cmd = p
            argv = tuple(cmd.split())
        if pid in me or not argv:
            continue
        if any("agentree" == os.path.basename(a) or a.endswith("/agentree") for a in argv[:2]):
            continue
        if is_opencode_argv(argv):
            out.append(pid)
    return out


# --------------------------------------------------------------------------- pure helpers

def port_from_argv(argv: tuple[str, ...] | list[str]) -> int | None:
    for i, a in enumerate(argv):
        val = None
        if a == "--port" and i + 1 < len(argv):
            val = argv[i + 1]
        elif a.startswith("--port="):
            val = a.split("=", 1)[1]
        if val is not None and val.isdigit() and int(val) > 0:
            return int(val)
    return None


class AttachTarget(NamedTuple):
    """`opencode attach <url> [--session|-s <id>] [--dir <dir>] ...`: a TUI client of a server
    (often another agent's embedded server). It listens on no port itself."""
    url: str
    port: int | None              # the server's port; None when unsupported (see error)
    session_id: str | None        # --session / -s, unless --fork (the shown session is a new fork)
    directory: str | None         # --dir as given (may be relative to the process cwd)
    error: str | None = None      # e.g. non-local host: no status


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# attach options that take a value (opencode 1.18.27 `attach --help`), global ones included
_ATTACH_VALUE_OPTS = frozenset({"--dir", "--session", "-s", "--password", "-p", "--username", "-u",
                                "--log-level", "--replay-limit"})


def parse_attach(argv: tuple[str, ...] | list[str]) -> AttachTarget | None:
    """The attach target of an opencode argv, None if it is not `opencode attach`."""
    opts: dict[str, str] = {}
    flags: set[str] = set()
    pos: list[str] = []
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            pos += list(argv[i + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            name, eq, val = a.partition("=")
            if name in _ATTACH_VALUE_OPTS:
                if eq:
                    opts[name] = val
                elif i + 1 < n:
                    opts[name] = argv[i + 1]
                    i += 1
            else:
                flags.add(name)
        else:
            pos.append(a)
        i += 1
    if not pos or pos[0] != "attach":
        return None
    url = pos[1] if len(pos) > 1 else ""
    sid = opts.get("--session", opts.get("-s")) or None
    if "--fork" in flags:
        sid = None
    directory = opts.get("--dir") or None
    try:
        u = urllib.parse.urlsplit(url)
        host, port = (u.hostname or "").lower(), u.port
    except ValueError:
        return AttachTarget(url, None, sid, directory, f"bad attach url {url[:80]!r}")
    if not url or u.scheme not in ("http", "https") or not host:
        return AttachTarget(url, None, sid, directory, f"bad attach url {url[:80]!r}")
    if host not in LOCAL_HOSTS:
        return AttachTarget(url, None, sid, directory, f"attach to non-local server {host} is not supported (no status)")
    if u.scheme != "http":
        return AttachTarget(url, None, sid, directory, f"attach over {u.scheme} is not supported (no status)")
    return AttachTarget(url, port or 80, sid, directory)


def map_status(*, api_ok: bool, pending_input: bool, session_status: str | None,
               last_activity_ms: int | None, busy_since_ms: int | None, now_ms: int,
               done_after_s: int = DONE_AFTER_S) -> tuple[AgentStatus, int | None]:
    """Agent status + seconds in that status.

    unknown      API unreachable
    needs_input  a permission or question request is pending
    busy         session status busy/retry (since the last user message)
    idle         otherwise, last activity < done_after_s ago
    done         otherwise (idle for longer than done_after_s)
    """
    def since(ms: int | None) -> int | None:
        return None if ms is None else max(0, (now_ms - int(ms)) // 1000)

    if not api_ok:
        return "unknown", None
    if pending_input:
        return "needs_input", since(last_activity_ms)
    if session_status in ("busy", "retry"):
        return "busy", since(busy_since_ms if busy_since_ms is not None else last_activity_ms)
    if last_activity_ms is None:
        return "idle", None
    age = since(last_activity_ms)
    return ("done" if age is not None and age >= done_after_s else "idle"), age


def _realpath(p: str | None) -> str | None:
    return os.path.realpath(p) if p else None


def pick_session(sessions: list[dict], cwd: str | None, statuses: dict[str, Any]) -> dict | None:
    """Root session for `cwd`: a busy one if any, else the most recently updated."""
    rc = _realpath(cwd)
    cands = [s for s in sessions if isinstance(s, dict) and not s.get("parentID")
             and (rc is None or _realpath(s.get("directory")) == rc)]
    if not cands:
        return None
    return _best(cands, statuses)


def _upd(s: dict) -> int:
    t = s.get("time") or {}
    return int(t.get("updated") or t.get("created") or 0)


def _best(cands: list[dict], statuses: dict[str, Any]) -> dict:
    """A busy session if any (per-process /session/status), else the most recently updated."""
    busy = [s for s in cands if (statuses.get(s.get("id")) or {}).get("type") in ("busy", "retry")]
    return max(busy or cands, key=_upd)


PANE_HOME_TITLE = "OpenCode"       # TUI pane title on the home screen (no session open)
PANE_SESSION_PREFIX = "OC | "      # TUI pane title with a session open: "OC | <session title>"
_ELLIPSES = ("...", "…")


def parse_pane_title(title: str | None) -> tuple[str, str | None] | None:
    """Classify an opencode TUI pane title.

    ("home", None)         home screen, no session open
    ("session", <title>)   a session is open; <title> may be truncated (trailing ... / …)
    None                   unavailable / not set by opencode (e.g. renamed pane)
    """
    if not title:
        return None
    t = title.strip()
    if t == PANE_HOME_TITLE:
        return ("home", None)
    if t.startswith(PANE_SESSION_PREFIX.rstrip()) and len(t) > len(PANE_SESSION_PREFIX.rstrip()):
        rest = t[len(PANE_SESSION_PREFIX.rstrip()):].strip()
        return ("session", rest) if rest else None
    return None


def match_session_title(sessions: list[dict], cwd: str | None, want: str,
                        statuses: dict[str, Any]) -> dict | None:
    """Root session whose title equals `want` (else starts with `want` minus a trailing
    truncation ellipsis). Sessions in `cwd` are preferred over other directories of the project;
    ties: busy, then most recently updated."""
    want = want.strip()
    stem = want
    for e in _ELLIPSES:
        if stem.endswith(e):
            stem = stem[: -len(e)].rstrip()
            break
    rc = _realpath(cwd)
    roots = [s for s in sessions if isinstance(s, dict) and not s.get("parentID")]
    here = [s for s in roots if rc is None or _realpath(s.get("directory")) == rc]

    def title(s: dict) -> str:
        return (s.get("title") or "").strip()

    tiers = [
        [s for s in here if title(s) == want],
        [s for s in roots if title(s) == want],
    ]
    if stem:
        tiers += [
            [s for s in here if title(s).startswith(stem)],
            [s for s in roots if title(s).startswith(stem)],
        ]
    for pool in tiers:
        if pool:
            return _best(pool, statuses)
    return None


def select_session(sessions: list[dict], cwd: str | None, statuses: dict[str, Any],
                   pane_title: str | None) -> tuple[dict | None, str | None]:
    """(session, title) for one opencode process.

    Several processes in the same project list the same sessions, so the zellij pane title
    (set by the TUI) disambiguates: home screen -> no session; "OC | <title>" -> the session with
    that title (if none matches, no session and the pane's title). Without a usable pane title,
    fall back to `pick_session` (busy in this process's /session/status, else most recent).
    """
    parsed = parse_pane_title(pane_title)
    if parsed is None:
        s = pick_session(sessions, cwd, statuses)
        return s, (s or {}).get("title") or None
    kind, want = parsed
    if kind == "home" or not want:
        return None, None
    s = match_session_title(sessions, cwd, want, statuses)
    if s is None:
        return None, want
    return s, s.get("title") or None


def message_tokens(info: dict) -> int | None:
    tok = info.get("tokens") or {}
    if not tok:
        return None
    total = tok.get("total")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    cache = tok.get("cache") or {}
    s = sum(int(tok.get(k) or 0) for k in ("input", "output", "reasoning")) + \
        int(cache.get("read") or 0) + int(cache.get("write") or 0)
    return s or None


def summarize_messages(msgs: list[dict]) -> dict:
    """From `/session/{id}/message` (oldest first) extract: last assistant text, context tokens
    (+ provider/model of that message), last activity ms, and when the current turn started."""
    out: dict = {"last_message": None, "context_tokens": None, "provider": None, "model": None,
                 "last_activity_ms": None, "last_user_ms": None}
    for m in reversed(msgs or []):
        info = m.get("info") or {}
        role = info.get("role")
        t = info.get("time") or {}
        ts = t.get("completed") or t.get("created")
        if ts and (out["last_activity_ms"] is None or ts > out["last_activity_ms"]):
            out["last_activity_ms"] = int(ts)
        if role == "user" and out["last_user_ms"] is None and t.get("created"):
            out["last_user_ms"] = int(t["created"])
        if role != "assistant":
            continue
        if out["context_tokens"] is None:
            n = message_tokens(info)
            if n:
                out["context_tokens"] = n
                out["provider"], out["model"] = info.get("providerID"), info.get("modelID")
        if out["last_message"] is None:
            texts = [p.get("text") or "" for p in (m.get("parts") or [])
                     if p.get("type") == "text" and not p.get("synthetic") and (p.get("text") or "").strip()]
            if texts:
                txt = texts[-1].strip()
                out["last_message"] = txt if len(txt) <= LAST_MESSAGE_MAX else txt[: LAST_MESSAGE_MAX - 1] + "…"
    return out


def parse_providers_limits(data: dict) -> dict[tuple[str, str], int]:
    out: dict[tuple[str, str], int] = {}
    for p in (data or {}).get("providers") or []:
        pid = p.get("id")
        models = p.get("models") or {}
        items = models.items() if isinstance(models, dict) else ((m.get("id"), m) for m in models)
        for mid, m in items:
            ctx = ((m or {}).get("limit") or {}).get("context")
            if pid and mid and isinstance(ctx, (int, float)) and ctx > 0:
                out[(pid, mid)] = int(ctx)
    return out


# --------------------------------------------------------------------------- http

class _ApiDown(Exception):
    pass


def _parse_response(buf: bytes) -> tuple[int, dict[str, str], bytes] | None:
    """(status, lower-cased headers, body so far) once the header block is complete."""
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        return None
    lines = buf[:end].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise _ApiDown(f"bad status line {lines[0][:80]!r}")
    headers: dict[str, str] = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(parts[1]), headers, buf[end + 4:]


def _dechunk(body: bytes) -> bytes | None:
    """Decode a chunked body; None if incomplete."""
    out = bytearray()
    i = 0
    while True:
        j = body.find(b"\r\n", i)
        if j < 0:
            return None
        size_s = body[i:j].split(b";", 1)[0].strip()
        try:
            size = int(size_s, 16)
        except ValueError:
            raise _ApiDown("bad chunked encoding") from None
        if size == 0:
            return bytes(out)
        if len(body) < j + 2 + size + 2:
            return None
        out += body[j + 2:j + 2 + size]
        i = j + 2 + size + 2


def _complete_body(headers: dict[str, str], body: bytes, eof: bool) -> bytes | None:
    if "chunked" in headers.get("transfer-encoding", "").lower():
        return _dechunk(body)
    cl = headers.get("content-length")
    if cl is not None and cl.isdigit():
        n = int(cl)
        if len(body) >= n:
            return body[:n]
        if eof:
            raise _ApiDown("truncated response")
        return None
    return body if eof else None


def http_get(port: int, target: str, timeout: float, max_bytes: int = MAX_RESPONSE) -> bytes:
    """GET http://127.0.0.1:<port><target> with a deadline on the *whole* exchange.

    Raw socket on purpose: never follows redirects (any non-200 status, 3xx included, is a
    failure), never consults proxy settings, never leaves 127.0.0.1, every recv is bounded by
    the remaining time, and the response is capped at `max_bytes`.
    """
    deadline = time.monotonic() + max(0.05, timeout)

    def remaining() -> float:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise _ApiDown("deadline exceeded")
        return rem

    req = (f"GET {target} HTTP/1.1\r\nHost: {HOST}:{int(port)}\r\nAccept: application/json\r\n"
           f"Accept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("latin-1")
    try:
        sock = socket.create_connection((HOST, int(port)), timeout=remaining())
    except OSError as e:
        raise _ApiDown(str(e)) from e
    try:
        sock.settimeout(remaining())
        sock.sendall(req)
        buf = bytearray()
        parsed = None
        while True:
            sock.settimeout(remaining())
            chunk = sock.recv(65536)
            eof = not chunk
            buf += chunk
            if len(buf) > max_bytes + 65536:
                raise _ApiDown(f"response larger than {max_bytes} bytes")
            if parsed is None:
                parsed = _parse_response(bytes(buf))
                if parsed is None:
                    if eof:
                        raise _ApiDown("connection closed before headers")
                    continue
                status, headers, _ = parsed
                if status != 200:
                    raise _ApiDown(f"HTTP {status}")   # 3xx included: redirects are never followed
            body = _parse_response(bytes(buf))[2]  # type: ignore[index]
            done = _complete_body(parsed[1], body, eof)
            if done is not None:
                if len(done) > max_bytes:
                    raise _ApiDown(f"response larger than {max_bytes} bytes")
                return done
            if eof:
                raise _ApiDown("truncated response")
    except TimeoutError as e:
        raise _ApiDown("deadline exceeded") from e
    except OSError as e:
        raise _ApiDown(str(e) or type(e).__name__) from e
    finally:
        sock.close()


ApiDown = _ApiDown          # public alias (events / doctor)
MAX_HEADER = 64 << 10


def valid_port(port: Any) -> int:
    """`port` as an int in 1..65535, else ApiDown."""
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise _ApiDown(f"bad port {port!r}") from None
    if not 0 < p < 65536:
        raise _ApiDown(f"bad port {port!r}")
    return p


def http_open(port: int, target: str, timeout: float, *, accept: str = "text/event-stream"
              ) -> tuple[socket.socket, dict[str, str], bytes]:
    """Open a streaming GET http://127.0.0.1:<port><target>; the deadline covers connect +
    request + the response header block only.

    Same rules as `http_get`: raw socket, 127.0.0.1 only (callers pass a port, never a host),
    no proxies, no redirects (any non-200 status, 3xx included, raises ApiDown). Returns
    (socket, lower-cased headers, body bytes already received); the caller owns the socket
    (blocking, timeout left at the remaining header deadline) and must close it.
    """
    p = valid_port(port)
    if not target.startswith("/"):
        raise _ApiDown(f"bad target {target[:80]!r}")
    deadline = time.monotonic() + max(0.05, timeout)

    def remaining() -> float:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise _ApiDown("deadline exceeded")
        return rem

    req = (f"GET {target} HTTP/1.1\r\nHost: {HOST}:{p}\r\nAccept: {accept}\r\n"
           f"Accept-Encoding: identity\r\nCache-Control: no-cache\r\n\r\n").encode("latin-1")
    try:
        sock = socket.create_connection((HOST, p), timeout=remaining())
    except OSError as e:
        raise _ApiDown(str(e) or type(e).__name__) from e
    ok = False
    try:
        sock.settimeout(remaining())
        sock.sendall(req)
        buf = bytearray()
        while True:
            sock.settimeout(remaining())
            chunk = sock.recv(65536)
            if not chunk:
                raise _ApiDown("connection closed before headers")
            buf += chunk
            parsed = _parse_response(bytes(buf))
            if parsed is None:
                if len(buf) > MAX_HEADER:
                    raise _ApiDown("response header block too large")
                continue
            status, headers, body = parsed
            if status != 200:
                raise _ApiDown(f"HTTP {status}")   # 3xx included: redirects are never followed
            ok = True
            return sock, headers, body
    except TimeoutError as e:
        raise _ApiDown("deadline exceeded") from e
    except OSError as e:
        raise _ApiDown(str(e) or type(e).__name__) from e
    finally:
        if not ok:
            sock.close()


def _get(port: int, path: str, params: dict | None, timeout: float) -> Any:
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    try:
        data = http_get(port, f"{path}{q}", timeout)
        return json.loads(data.decode("utf-8", "replace"))
    except _ApiDown as e:
        raise _ApiDown(f"{path}: {e}") from e
    except ValueError as e:
        raise _ApiDown(f"{path}: bad JSON: {e}") from e


# --------------------------------------------------------------------------- per agent

def _shows(parsed: tuple[str, str | None], sess: dict) -> bool:
    """Does a parsed pane title (see parse_pane_title) show session `sess`?"""
    kind, want = parsed
    if kind != "session" or not want:
        return False
    title = (sess.get("title") or "").strip()
    stem = want.strip()
    for e in _ELLIPSES:
        if stem.endswith(e):
            stem = stem[: -len(e)].rstrip()
            break
    return title == want.strip() or bool(stem) and title.startswith(stem)


def _busy(statuses: dict, sid: str | None) -> bool:
    return bool(sid) and (statuses.get(sid) or {}).get("type") in ("busy", "retry")


def _attached_state(port: int, sid: str, params: dict | None, left: Callable[[], float]
                    ) -> tuple[dict | None, dict, bool, str | None]:
    """(session, statuses, pending input, warning) for one known session id on a (shared) server:
    the session itself (GET /session/{id}), its status, and permission/question requests of it or
    its direct children (subagents). Other sessions of the server never count."""
    q = urllib.parse.quote(sid, safe="")
    warn = None
    try:
        sess = _get(port, f"/session/{q}", params, left())
    except _ApiDown as e:
        if "HTTP 404" not in str(e):
            raise
        sess, warn = None, f"session {sid} not found on :{port}"
    statuses = _get(port, "/session/status", params, left())
    if not isinstance(statuses, dict):
        statuses = {}
    if not isinstance(sess, dict) or sess.get("id") != sid:
        sess = None
    items: list = []
    for path in ("/permission", "/question"):
        try:
            got = _get(port, path, params, left())
            items += [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []
        except _ApiDown as e:
            warn = str(e)
    scope = {sid}
    others = {k for k, v in statuses.items() if k != sid and isinstance(v, dict)
              and v.get("type") in ("busy", "retry")} | {x.get("sessionID") for x in items} - {sid, None}
    if others and left() > 0.05:          # only then is it worth asking which are our subagents
        try:
            kids = _get(port, f"/session/{q}/children", params, left())
            scope |= {k.get("id") for k in kids if isinstance(k, dict) and k.get("id")} if isinstance(kids, list) else set()
        except _ApiDown as e:
            warn = str(e)
    pending = any(x.get("sessionID") in scope for x in items)
    if not _busy(statuses, sid) and any(_busy(statuses, k) for k in scope):
        statuses = {**statuses, sid: {"type": "busy"}}   # a subagent of this session is running
    return sess, statuses, pending, warn


def make_agent(*, pid: int, cwd: str | None, port: int | None, session_name: str | None,
               pane_id: int | None, start_time: float | None, api_ok: bool = False, **kw: Any) -> Agent:
    """An opencode Agent: meta {"port", "api_ok"}; status quality exact when the API answered,
    else unavailable ("Live status unavailable")."""
    quality: dict[str, Any] = ({"status_quality": "exact"} if api_ok else
                               {"status_quality": "unavailable", "status_note": UNAVAILABLE_NOTE})
    return Agent(pid=pid, cwd=cwd, session_name=session_name, pane_id=pane_id, start_time=start_time,
                 provider=SOURCE, meta={"port": port, "api_ok": bool(api_ok)}, **quality, **kw)


def agent_port(a: Any) -> Any:
    """The server port of an opencode Agent (meta["port"]), None if unknown."""
    meta = getattr(a, "meta", None)
    return meta.get("port") if isinstance(meta, Mapping) else None


def _agent(pid: int, argv: tuple[str, ...], env: dict[str, str], cwd: str | None, port: int | None,
           timeout: float, pane_title: str | None = None, start: float | None = None,
           attach: AttachTarget | None = None) -> tuple[Agent, str | None]:
    """One agent. `attach` (an `opencode attach` client): `port` is its server's, `cwd` its --dir
    (else process cwd), and with a --session id status/title come from that session only."""
    pane = env.get("ZELLIJ_PANE_ID")
    base: dict[str, Any] = dict(pid=pid, cwd=_realpath(cwd), port=port,
                                session_name=env.get("ZELLIJ_SESSION_NAME") or None,
                                pane_id=int(pane) if pane and pane.isdigit() else None, start_time=start)
    if attach is not None and attach.error:
        return make_agent(**base), f"pid {pid}: {attach.error}"
    if port is None:
        return make_agent(**base), f"pid {pid}: no listening port"
    deadline = time.monotonic() + timeout

    def left() -> float:
        return min(HTTP_TIMEOUT, deadline - time.monotonic())

    params = {"directory": cwd} if cwd else None
    warn = None
    if attach is not None:
        # an attach client shares its server with other clients: only its own session counts.
        # That is --session, unless the pane title (set by the TUI) shows another session (the
        # user switched) or there is no id (-c / --fork): then the pane title picks it
        try:
            sid = attach.session_id
            sess = None
            if sid:
                sess, statuses, pending, warn = _attached_state(port, sid, params, left)
            shown = parse_pane_title(pane_title)
            if not sid or (sess is not None and shown is not None and not _shows(shown, sess)):
                sessions = _get(port, "/session", {**(params or {}), "roots": "true", "limit": "20"}, left())
                statuses = _get(port, "/session/status", params, left())
                pick, _t = select_session(sessions if isinstance(sessions, list) else [], cwd,
                                          statuses if isinstance(statuses, dict) else {}, pane_title)
                sid = (pick or {}).get("id")
                sess, statuses, pending = None, {}, False
                if sid:
                    sess, statuses, pending, warn = _attached_state(port, sid, params, left)
        except _ApiDown as e:
            return make_agent(**base), f"pid {pid} attach :{port}: API unreachable ({e})"
        if warn:
            warn = f"pid {pid}: {warn}"
        title = (sess or {}).get("title") or None
        sess_status = (statuses.get(sid) or {}).get("type") if sess is not None else None
    else:
        try:
            sessions = _get(port, "/session", {**(params or {}), "roots": "true", "limit": "20"}, left())
            statuses = _get(port, "/session/status", params, left())
        except _ApiDown as e:
            return make_agent(**base), f"pid {pid} port {port}: API unreachable ({e})"
        if not isinstance(sessions, list):
            sessions = []
        if not isinstance(statuses, dict):
            statuses = {}
        pending = False
        for path in ("/permission", "/question"):
            try:
                items = _get(port, path, params, left())
                if isinstance(items, list) and items:
                    pending = True
            except _ApiDown as e:
                warn = f"pid {pid}: {e}"
        sess, title = select_session(sessions, cwd, statuses, pane_title)
        sess_status = (statuses.get(sess["id"]) or {}).get("type") if sess else None
        any_busy = any((v or {}).get("type") in ("busy", "retry") for v in statuses.values() if isinstance(v, dict))
        if any_busy and sess_status not in ("busy", "retry"):
            sess_status = "busy"  # e.g. only a child (subagent) session is listed as busy
    now_ms = int(time.time() * 1000)
    summary: dict = {}
    if sess is not None:
        try:
            msgs = _get(port, f"/session/{urllib.parse.quote(sess['id'], safe='')}/message",
                        {**(params or {}), "limit": str(MESSAGE_LIMIT)}, left())
            summary = summarize_messages(msgs if isinstance(msgs, list) else [])
        except _ApiDown as e:
            warn = f"pid {pid}: {e}"
    last_activity = summary.get("last_activity_ms")
    if sess is not None:
        upd = (sess.get("time") or {}).get("updated")
        if upd and (last_activity is None or upd > last_activity):
            last_activity = int(upd)
    status, since = map_status(api_ok=True, pending_input=pending, session_status=sess_status,
                               last_activity_ms=last_activity, busy_since_ms=summary.get("last_user_ms"),
                               now_ms=now_ms)
    limit = None
    if summary.get("provider") and summary.get("model"):
        key = (pid, port)
        with _limits_lock:
            limits = _limits_cache.get(key)
        if limits is None and left() > 0.05:
            try:
                limits = parse_providers_limits(_get(port, "/config/providers", params, left()))
                with _limits_lock:
                    _limits_cache[key] = limits
            except _ApiDown:
                limits = None
        if limits:
            limit = limits.get((summary["provider"], summary["model"]))
    return make_agent(**base, status=status, status_since_s=since,
                      title=title, session_id=(sess or {}).get("id"),
                      last_message=summary.get("last_message"), context_tokens=summary.get("context_tokens"),
                      context_limit=limit, api_ok=True), warn


type PaneTitles = Mapping[tuple[str, int], str]


def _pane_key(env: dict[str, str]) -> tuple[str, int] | None:
    sname, pane = env.get("ZELLIJ_SESSION_NAME"), env.get("ZELLIJ_PANE_ID")
    if sname and pane and pane.isdigit():
        return (sname, int(pane))
    return None


def fetch(timeout: float = 1.0, *, pane_titles: PaneTitles | Callable[[], PaneTitles] | None = None,
          pids: Iterable[int] | None = None) -> tuple[tuple[Agent, ...], list[SourceWarning]]:
    """All running opencode agents.

    `pane_titles` maps (zellij session name, terminal pane id) -> pane title and is used to tell
    apart processes sharing a directory (see `select_session`). It may be a zero-arg callable,
    resolved only after process discovery so the caller can overlap it with its own fetch.

    `pids` (live-status fast path): skip `ps` discovery and look only at these known processes;
    ones that exited (or are no longer opencode servers) are dropped.
    """
    warnings: list[SourceWarning] = []
    infos: dict[int, tuple[tuple[str, ...], dict[str, str]]] = {}
    if pids is not None:
        for pid in dict.fromkeys(int(p) for p in pids):
            ae = procmod.proc_args_env(pid)
            if ae:
                infos[pid] = ae
        pids = list(infos)
        if not pids:
            return (), warnings
    else:
        ps = procmod.list_procs(timeout=timeout)
        if not ps:
            return (), [SourceWarning(SOURCE, "ps failed")]
        pids = find_opencode(ps)
        if not pids:
            return (), warnings
        cmd_by_pid = {p: c for p, _pp, c in ps}
        for pid in pids:
            ae = procmod.proc_args_env(pid)
            infos[pid] = ae if ae else (tuple(cmd_by_pid.get(pid, "").split()), {})
    # recheck with the precise argv (ps output is space-split)
    pids = [p for p in pids if is_opencode_argv(infos[p][0])]
    cwds = dict(procmod.proc_cwd(pids, timeout=timeout))
    starts = procmod.start_times(pids)          # (pid, start_time) = identity across pid reuse
    attach = {p: at for p in pids if (at := parse_attach(infos[p][0])) is not None}
    ports = {p: attach[p].port if p in attach else port_from_argv(infos[p][0]) for p in pids}
    for p, at in attach.items():          # an attach client works in --dir (else its own cwd)
        if at.directory:
            cwds[p] = os.path.normpath(os.path.join(cwds.get(p) or "/", os.path.expanduser(at.directory)))
    need = [p for p, port in ports.items() if port is None and p not in attach]
    if need:
        lp = procmod.listening_ports(need, timeout=timeout)
        for p in need:
            if lp.get(p):
                ports[p] = lp[p][0]
    titles: PaneTitles = {}
    if callable(pane_titles):
        try:
            titles = pane_titles() or {}
        except Exception as e:  # noqa: BLE001 - titles are optional
            warnings.append(SourceWarning(SOURCE, f"pane titles unavailable: {e}"))
    elif pane_titles:
        titles = pane_titles

    def title_of(p: int) -> str | None:
        key = _pane_key(infos[p][1])
        return titles.get(key) if key is not None else None

    def job(p: int):
        return lambda: _agent(p, infos[p][0], infos[p][1], cwds.get(p), ports[p], timeout, title_of(p),
                              starts.get(p), attach=attach.get(p))

    # every _agent call is bounded by `timeout` (whole-response HTTP deadlines)
    results = _par.run_all([job(p) for p in pids], workers=16, deadline=time.monotonic() + timeout + 0.5,
                           name="agentree-opencode")
    agents = []
    for p, out in zip(pids, results):
        if out[0] == _par.OK:
            a, w = out[1]
        else:
            env = infos[p][1]
            pane = env.get("ZELLIJ_PANE_ID")
            a = make_agent(pid=p, cwd=_realpath(cwds.get(p)), port=ports[p],
                           session_name=env.get("ZELLIJ_SESSION_NAME") or None,
                           pane_id=int(pane) if pane and pane.isdigit() else None, start_time=starts.get(p))
            w = f"pid {p}: {'timed out' if out[0] == _par.PENDING else out[1]}"
        agents.append(a)
        if w:
            warnings.append(SourceWarning(SOURCE, w))
    agents.sort(key=lambda a: a.pid)
    return tuple(agents), warnings


# --------------------------------------------------------------------------- launch helpers

def split_cmd(cmd: str | Sequence[str]) -> list[str]:
    """A command (string or argv) as argv, argv0 `~`-expanded (spawn's _split_cmd)."""
    if isinstance(cmd, str):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = [cmd]
        parts = parts or [cmd]
    else:
        parts = list(cmd)
    return [os.path.expanduser(parts[0]), *parts[1:]] if parts else [""]


def launch_args(prompt: str | None) -> list[str]:
    """Arguments appended to the configured command: `--prompt <prompt>` when given."""
    return ["--prompt", prompt] if prompt else []


# --------------------------------------------------------------------------- doctor

DOC_TIMEOUT = 1.2
DOC_MAX = 16 << 20
POLL_S, IDLE_S = 2.0, 10.0       # gather.LiveStatus.POLL_EVERY / OpencodeProvider IDLE_EVERY
_NOT_SERVER_FLAGS = frozenset({"--version", "-v", "--help", "-h"})


def discover_servers(probe: Any, timeout: float) -> list[tuple]:
    """[(pid, port, cwd, attach)] of running opencode processes (same discovery as `fetch`),
    through the generic process primitives of a diagnostics Probe. `attach` is an AttachTarget
    for `opencode attach` clients (port = their server's), else None."""
    ps = probe.list_procs(timeout=timeout)
    pids = find_opencode(ps)
    cmd_by_pid = {p: c for p, _pp, c in ps}
    argv: dict[int, tuple[str, ...]] = {}
    for p in pids:
        ae = probe.proc_args_env(p)
        argv[p] = ae[0] if ae else tuple(cmd_by_pid.get(p, "").split())
    pids = [p for p in pids if is_opencode_argv(argv[p])
            and not _NOT_SERVER_FLAGS.intersection(argv[p][1:])]   # e.g. our own `opencode --version`
    attach = {p: parse_attach(argv[p]) for p in pids}
    ports = {p: attach[p].port if attach[p] else port_from_argv(argv[p]) for p in pids}
    need = [p for p, port in ports.items() if port is None and not attach[p]]
    if need:
        lp = probe.listening_ports(need, timeout=timeout)
        for p in need:
            if lp.get(p):
                ports[p] = lp[p][0]
    cwds = probe.proc_cwd(pids, timeout=timeout)
    return [(p, ports[p], cwds.get(p), attach[p]) for p in sorted(pids)]


def _sse_probe(port: int, cwd: str | None, timeout: float) -> str:
    from agentree.providers import _opencode_events as ev   # lazy

    return ev.probe(port, cwd, timeout)


@dataclass
class DoctorProbe:
    """opencode-local doctor access (the generic Probe has no opencode fields).
    `servers(timeout)` -> [(pid, port, cwd, attach)]; None = discover_servers via the generic Probe.
    `http_get(port, target, timeout, max_bytes)` -> bytes; `sse_probe(port, cwd, timeout)` -> str."""
    servers: Callable[[float], list[tuple]] | None = None
    http_get: Callable[..., bytes] = http_get
    sse_probe: Callable[..., str] = _sse_probe


def check_opencode(cfg: Any, probe: Any, local: DoctorProbe, spec: ProviderSpec) -> list[Check]:
    from agentree import diagnostics as dg

    S = spec.id
    out: list[Check] = []
    from agentree.providers.command import configured_command

    raw = configured_command(spec, cfg)
    raw = raw if raw is not None else spec.default_command
    cmd = (raw.split() or [""])[0]
    exp = dg.expand(cmd, probe)
    if (os.path.isabs(exp) and probe.is_exec(exp)) or (not os.path.isabs(exp) and probe.which(exp)):
        out.append(dg.Check(S, "ok", f"{spec.command_key} {raw}"))
    else:
        out.append(dg.Check(S, "warn", f"{spec.command_key} {raw}: not an executable (o / new agent will fail)"))
    try:
        servers = (local.servers or (lambda t: discover_servers(probe, t)))(dg.CMD_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - reported
        return out + [dg.Check(S, "warn", f"cannot list opencode processes: {e}")]
    if not servers:
        return out + [dg.Check(S, "ok", "no running opencode servers"),
                      dg.Check(S, "ok", f"live status: nothing to watch; the popup polls every {POLL_S:g} s")]
    sse_ok: list[int] = []

    def doc(who: str, port: int) -> Check:
        try:
            d = json.loads(local.http_get(port, "/doc", DOC_TIMEOUT, DOC_MAX).decode("utf-8", "replace"))
            paths = d.get("paths") or {}
            ver = f"OpenAPI {d.get('openapi') or '?'}"
            if "/event" not in paths:
                return dg.Check(S, "warn", f"{who}: /doc ok ({ver}) but it lists no /event endpoint")
            return dg.Check(S, "ok", f"{who}: /doc ok ({ver}, {len(paths)} paths)")
        except (ApiDown, ValueError, AttributeError) as e:
            return dg.Check(S, "fail", f"{who}: /doc unreachable: {e}")

    def sse(who: str, port: int, cwd: str | None) -> Check:
        try:
            c = dg.Check(S, "ok", f"{who}: SSE /event ok ({local.sse_probe(port, cwd, dg.CMD_TIMEOUT)})")
            sse_ok.append(port)
            return c
        except ApiDown as e:
            return dg.Check(S, "fail", f"{who}: SSE /event failed: {e}")

    # one /doc + SSE probe per port: `opencode attach` clients share their server's port (and its
    # event stream, see _opencode_events.EventWatcher), so they are reported, not probed again
    procs = [tuple(x) + (None,) * (4 - len(x)) for x in servers]
    owner: dict[int, int] = {}                  # port -> pid whose line carries the probes
    for pid, port, _cwd, at in sorted(procs, key=lambda x: (x[3] is not None, x[0])):
        if port is not None and not (at is not None and at.error):
            owner.setdefault(port, pid)
    jobs: list[Callable[[], Check]] = []
    unwatchable = 0
    for pid, port, cwd, at in procs:
        if at is not None:
            if at.error or port is None:
                unwatchable += 1
                jobs.append(lambda pid=pid, at=at: dg.Check(S, "warn", f"pid {pid}: attach {at.url}: {at.error or 'no port'}"))
                continue
            sess = f" (session {at.session_id})" if at.session_id else ""
            jobs.append(lambda pid=pid, port=port, sess=sess: dg.Check(S, "ok", f"pid {pid}: attach → :{port}{sess}"))
            if owner[port] != pid:
                continue
            who = f"pid {pid} attach :{port}"    # the server itself is not a local opencode process
        else:
            who = f"pid {pid}" + (f" :{port}" if port else "")
            if port is None:
                unwatchable += 1
                jobs.append(lambda who=who: dg.Check(S, "warn", f"{who}: no listening port (status unavailable)"))
                continue
            if owner[port] != pid:
                jobs.append(lambda who=who, port=port: dg.Check(S, "ok", f"{who}: shares :{port} with pid {owner[port]}"))
                continue
        jobs.append(lambda who=who, port=port: doc(who, port))
        jobs.append(lambda who=who, port=port, cwd=cwd: sse(who, port, cwd))
    results = _par.run_all(jobs, workers=32, deadline=time.monotonic() + DOC_TIMEOUT + 0.2, name="agentree-doctor")
    for o in results:
        out.append(o[1] if o[0] == _par.OK else
                   dg.Check(S, "fail", f"opencode check {'timed out' if o[0] == _par.PENDING else o[1]}"))
    n, k = len(owner) + unwatchable, len(sse_ok)
    if k == n:
        out.append(dg.Check(S, "ok", f"live status: event streams on {k}/{n} server(s); the popup refreshes "
                                     f"agents on events, everything else every {IDLE_S:g} s"))
    elif k:
        out.append(dg.Check(S, "warn", f"live status: event streams on {k}/{n} server(s); the popup polls every "
                                       f"{POLL_S:g} s while any stream is down"))
    else:
        out.append(dg.Check(S, "warn", f"live status: no event stream reachable; the popup polls every {POLL_S:g} s"))
    return out


# --------------------------------------------------------------------------- provider

def _port_ok(port: Any) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536


def _in_panes(env: Mapping[str, str], session: str, pane_ids: frozenset[int] | set[int]) -> bool:
    pane = env.get("ZELLIJ_PANE_ID", "")
    return env.get("ZELLIJ_SESSION_NAME") == session and pane.isdigit() and int(pane) in pane_ids


def _event_watcher(on_change: Callable[[int, str], None]) -> Any:
    from agentree.providers._opencode_events import EventWatcher   # lazy: after the first frame

    return EventWatcher(on_change)


class OpencodeProvider:
    """The opencode provider (see agentree.providers.base.Provider).

    `watcher_factory(on_change(pid, reason))` builds the live-status watcher (default: the SSE
    EventWatcher, imported lazily); `doctor_probe` the opencode-local doctor access."""

    quick_failure: Literal["keep_snapshot", "keep_provider"] = "keep_snapshot"
    IDLE_EVERY = 10.0                # every agent's server stream is connected
    POLL_EVERY = 2.0                 # otherwise

    def __init__(self, spec: ProviderSpec, *, watcher_factory: Callable[..., Any] | None = None,
                 doctor_probe: DoctorProbe | None = None) -> None:
        self.spec = spec
        self.id = spec.id
        self._watcher_factory = watcher_factory
        self.doctor_probe = doctor_probe

    # -- processes
    def matches_argv(self, argv: Sequence[str]) -> bool:
        return is_opencode_argv(tuple(argv or ()))

    def find_pids(self, rows: Sequence[Any]) -> tuple[int, ...]:
        return tuple(find_opencode(rows))

    def session_id_from_argv(self, argv: Sequence[str]) -> str | None:
        at = parse_attach(tuple(argv or ()))
        return at.session_id if at is not None else None

    # -- launch
    def resolve_command(self, cfg: Any) -> tuple[str, ...] | None:
        from agentree.providers.command import resolve_for

        return resolve_for(self.spec, cfg)

    def build_launch(self, cfg: Any, request: LaunchRequest) -> Launch:
        """pane: the resolved executable (no extra args). spawn: without a prompt no command (the
        user's named layout, e.g. agent.kdl, starts opencode); with one, the configured command
        (as configured, argv0 `~`-expanded) + `--prompt <prompt>`."""
        if request.mode == "pane":
            cmd = self.resolve_command(cfg)
            if cmd is None:
                from agentree.providers import registry

                return Launch(argv=None, errors=(registry.missing_command_message(self.id, cfg),))
            return Launch(argv=(*cmd, *launch_args(request.prompt or None)))
        if not request.prompt:
            return Launch(argv=None)
        from agentree.providers.command import configured_command

        raw = configured_command(self.spec, cfg)
        argv = split_cmd(raw if raw is not None else self.spec.default_command)
        return Launch(argv=(*argv, *launch_args(request.prompt)))

    def execute_setup(self, step: SetupStep, *, runner: Runner, state_dir: str) -> str:
        raise ValueError(f"{self.id}: unknown setup step {step.operation!r}")

    # -- status
    def fetch_full(self, ctx: FetchContext) -> FetchResult:
        return fetch(timeout=ctx.timeout_s, pane_titles=ctx.pane_titles)

    def fetch_quick(self, agents: Sequence[Agent], ctx: FetchContext) -> FetchResult:
        return fetch(timeout=ctx.timeout_s, pane_titles=ctx.pane_titles, pids=[a.pid for a in agents])

    def fetch_budget_s(self, timeout_s: float, *, quick: bool) -> float:
        # full: ps, cwd/ports, then HTTP per agent (each phase bounded by t); quick: no ps
        return (2 if quick else 3) * timeout_s

    def start_watcher(self, poke: Callable[[], None]) -> Watcher | None:
        factory = self._watcher_factory or _event_watcher
        return factory(lambda pid, reason: poke())

    def refresh_policy(self, agents: Sequence[Agent], watcher: Watcher | None) -> RefreshPolicy:
        """IDLE_EVERY only when every agent has a valid port and a connected stream (an `opencode
        attach` client counts as covered by its server's stream), else POLL_EVERY; no quick timer."""
        if watcher is None:
            return RefreshPolicy(self.POLL_EVERY)
        h = watcher.healthy()
        covered = all(_port_ok(agent_port(a)) and h.get(a.pid) is True for a in agents)
        return RefreshPolicy(self.IDLE_EVERY if covered else self.POLL_EVERY)

    def find_launched_pids(self, session: str, pane_ids: frozenset[int], *, session_id: str | None,
                           timeout_s: float) -> tuple[int, ...]:
        """PIDs of opencode agents running in `pane_ids` of zellij session `session`."""
        rows = procmod.list_procs(timeout=timeout_s)
        hits: list[int] = []
        for pid in self.find_pids(rows):
            ae = procmod.proc_args_env(pid)
            if ae and _in_panes(ae[1], session, pane_ids):
                hits.append(pid)
        return tuple(sorted(hits))

    # -- doctor / cli / demo
    def doctor_jobs(self, cfg: Any, probe: Probe, *, local: DoctorProbe | None = None) -> tuple[DoctorJob, ...]:
        from agentree import diagnostics as dg

        loc = local or self.doctor_probe or DoctorProbe()
        return (DoctorJob("versions", lambda: dg.check_tool("opencode", probe, missing="warn")),
                DoctorJob(self.id, lambda: check_opencode(cfg, probe, loc, self.spec)))

    def run_internal(self, command: str, argv: Sequence[str]) -> int:
        raise KeyError(command)

    def demo_agents(self, ctx: DemoContext) -> tuple[DemoAgent, ...]:
        p = ctx.paths
        sso, dot, simd, rate = p["sso"], p["dotfiles"], p["simd"], p["rate_limit"]

        def pane(pane_id: int, tab_id: int, cwd: str, port: int) -> ZPane:
            return ZPane(pane_id=pane_id, is_plugin=False, tab_id=tab_id, title="opencode", command="opencode",
                         argv=("opencode", "--port", str(port), "--auto"), cwd=cwd)

        return (
            DemoAgent(make_agent(pid=4101, cwd=sso, port=5000, session_name="web-app", pane_id=2, start_time=None,
                                 status="busy", status_since_s=120, title="Refactor token refresh",
                                 session_id="ses_a1",
                                 last_message="Moved the refresh logic into useTokenRefresh and updated the three "
                                              "call sites. Running the auth unit tests next.",
                                 context_tokens=12_345, context_limit=200_000, api_ok=True),
                      "web-app", 1, pane(2, 1, sso, 5000)),
            DemoAgent(make_agent(pid=4102, cwd=sso, port=5001, session_name="web-app", pane_id=3, start_time=None,
                                 status="idle", status_since_s=600, title="Write refresh tests", session_id="ses_a2",
                                 last_message="Added 6 tests for token refresh; all passing.",
                                 context_tokens=48_000, context_limit=200_000, api_ok=True),
                      "web-app", 1, pane(3, 1, sso, 5001)),
            DemoAgent(make_agent(pid=4201, cwd=dot, port=5002, session_name="dotfiles", pane_id=1, start_time=None,
                                 status="needs_input", status_since_s=300, title="Migrate fish abbreviations",
                                 session_id="ses_b1", last_message="May I overwrite fish/conf.d/abbr.fish?",
                                 api_ok=True),
                      "dotfiles", 10, pane(1, 10, dot, 5002)),
            DemoAgent(make_agent(pid=4301, cwd=simd, port=5003, session_name="engine", pane_id=2, start_time=None,
                                 status="done", status_since_s=3 * 3600, title="Profile hash table",
                                 session_id="ses_c1", api_ok=True),
                      "engine", 21, pane(2, 21, simd, 5003)),
            DemoAgent(make_agent(pid=4401, cwd=rate, port=5004, session_name=None, pane_id=None, start_time=None,
                                 status="busy", status_since_s=600, title="Add rate limiter", session_id="ses_d1",
                                 api_ok=True),
                      None, None, None),
        )


def create_provider(spec: ProviderSpec) -> OpencodeProvider:
    return OpencodeProvider(spec)
