"""Live opencode status via the server-sent event stream (opencode 1.18.27, verified live).

Endpoints (OpenAPI `/doc`):
- GET /event?directory=<abs>   per-instance stream (`event.subscribe`); frames are
                               `data: {"id":"evt_…","type":"<type>","properties":{…}}` + blank line,
                               one HTTP chunk per frame (Transfer-Encoding: chunked), no `event:`
                               field. First frame `server.connected`; `server.heartbeat` every 10 s.
- GET /global/event            all instances; same frames wrapped as `{"payload": {…}}`.

Status-relevant types: session.status ({sessionID, status:{type: busy|idle|retry}}), session.idle,
session.updated (title / time.updated), session.created|deleted|error|compacted,
permission.asked|replied (+ .v2.), question.asked|replied|rejected (+ .v2.), server.connected,
server.instance.disposed, global.disposed. Streaming noise (message.part.delta,
message.part.updated, session.next.*, session.diff, server.heartbeat) is ignored.

`EventWatcher` keeps one daemon thread per server port (agents sharing a server, e.g. a TUI and
`opencode attach` clients of it, share the stream; events are routed to them by sessionID) and
calls `on_change(pid, reason)` at most once per `debounce_s` per stream (leading edge + one
trailing call).
Sockets are 127.0.0.1-only: the API takes ports, never hosts (see `opencode.http_open`).
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.parse
from typing import Callable, Iterable, NamedTuple

from agentree.providers import opencode as oc
from agentree.types import Agent

EVENT_PATH = "/event"
GLOBAL_EVENT_PATH = "/global/event"
CONNECT_TIMEOUT = 1.0
DEBOUNCE_S = 0.15
BACKOFF = (0.5, 5.0)
STALL_S = 25.0            # opencode sends server.heartbeat every 10 s
GIVE_UP_S = 10.0          # connection refused this long (or pid dead) => port gone
RETRY_GONE_S = 30.0       # a given-up watch of an agent still listed is restarted after this long
MAX_TRAILER = 8 << 10     # chunked trailer section larger than this is a protocol error
RECV_TICK = 0.5
MAX_EVENT = 8 << 20       # bytes of data per event; bigger events are dropped
JSON_MAX = 256 << 10      # bigger payloads: type sniffed from the prefix instead of json.loads

STATUS_TYPES = frozenset({
    "server.connected", "server.instance.disposed", "global.disposed",
    "session.status", "session.idle", "session.updated", "session.created", "session.deleted",
    "session.error", "session.compacted",
})
STATUS_PREFIXES = ("permission.", "question.")


class StreamError(Exception):
    pass


# --------------------------------------------------------------------------- pure: SSE

class SseEvent(NamedTuple):
    event: str                 # SSE `event:` field ("message" when absent)
    data: str                  # `data:` lines joined with "\n"
    id: str | None = None


class SseParser:
    """Incremental text/event-stream parser (WHATWG rules): CRLF / LF / CR line endings, split
    anywhere across feeds, multi-line `data:`, `event:`, `id:`, comments (`:` lines, counted in
    `comments`), BOM. Events whose data exceeds `max_event` bytes are dropped."""

    def __init__(self, max_event: int = MAX_EVENT) -> None:
        self.max_event = max_event
        self.comments = 0
        self._buf = b""
        self._skip_lf = False
        self._discard_line = False
        self._first = True
        self._reset()
        self._last_id: str | None = None

    def _reset(self) -> None:
        self._data: list[str] = []
        self._size = 0
        self._event = ""
        self._overflow = False

    def feed(self, chunk: bytes | str) -> list[SseEvent]:
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not chunk:
            return []
        if self._skip_lf:
            self._skip_lf = False
            if chunk[:1] == b"\n":
                chunk = chunk[1:]
        buf = self._buf + chunk if self._buf else chunk
        out: list[SseEvent] = []
        i, n = 0, len(buf)
        while i < n:
            jn, jr = buf.find(b"\n", i), buf.find(b"\r", i)
            j = jn if jr < 0 else jr if jn < 0 else min(jn, jr)
            if j < 0:
                break
            line = buf[i:j]
            if buf[j] == 13:  # \r
                if j + 1 < n:
                    i = j + 2 if buf[j + 1] == 10 else j + 1
                else:
                    i = j + 1
                    self._skip_lf = True
            else:
                i = j + 1
            if self._discard_line:
                self._discard_line = False
                continue
            ev = self._line(line)
            if ev is not None:
                out.append(ev)
        rest = buf[i:]
        if len(rest) > self.max_event:   # unterminated monster line: drop it and its event
            rest = b""
            self._discard_line = True
            self._overflow = True
        self._buf = rest
        return out

    def _line(self, raw: bytes) -> SseEvent | None:
        line = raw.decode("utf-8", "replace")
        if self._first:
            self._first = False
            if line.startswith("\ufeff"):
                line = line[1:]
        if not line:
            ev = None
            if self._data and not self._overflow:
                ev = SseEvent(self._event or "message", "\n".join(self._data), self._last_id)
            self._reset()
            return ev
        if line.startswith(":"):
            self.comments += 1
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._size += len(value) + 1
            if self._size > self.max_event:
                self._overflow = True
                self._data = []
            elif not self._overflow:
                self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id" and "\0" not in value:
            self._last_id = value
        return None


def parse_sse(chunks: Iterable[bytes | str]) -> list[SseEvent]:
    """All complete events in a stream given as arbitrary chunks (a trailing event without its
    terminating blank line is not dispatched)."""
    p = SseParser()
    out: list[SseEvent] = []
    for c in chunks:
        out.extend(p.feed(c))
    return out


_TYPE_RE = re.compile(r'"type"\s*:\s*"([^"\\]{1,200})"')


def event_type(evt: SseEvent) -> str | None:
    """opencode event type: the SSE `event:` field if set, else `type` of the JSON data
    (unwrapping `/global/event`'s {"payload": …})."""
    if evt.event and evt.event != "message":
        return evt.event
    d = evt.data
    if len(d) <= JSON_MAX:
        try:
            obj = json.loads(d)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            if isinstance(obj.get("payload"), dict):
                obj = obj["payload"]
            t = obj.get("type")
            return t if isinstance(t, str) else None
    m = _TYPE_RE.search(d[:512])
    return m.group(1) if m else None


def is_status_event(evt: SseEvent) -> bool:
    t = event_type(evt)
    return bool(t) and (t in STATUS_TYPES or t.startswith(STATUS_PREFIXES))  # type: ignore[union-attr]


_SID_RE = re.compile(r'"sessionID"\s*:\s*"([^"\\]{1,200})"')


def event_session(evt: SseEvent) -> str | None:
    """The opencode session an event is about: properties.sessionID (session.status, permission.*,
    question.*, message.*), else properties.info.id / info.sessionID (session.updated|created|
    deleted); None for server events or when unknown."""
    d = evt.data
    if len(d) <= JSON_MAX:
        try:
            obj = json.loads(d)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        if isinstance(obj.get("payload"), dict):
            obj = obj["payload"]
        props = obj.get("properties")
        if not isinstance(props, dict):
            return None
        sid = props.get("sessionID")
        info = props.get("info")
        if not isinstance(sid, str) and isinstance(info, dict):
            sid = info.get("sessionID") if isinstance(info.get("sessionID"), str) else info.get("id")
        return sid if isinstance(sid, str) and sid else None
    m = _SID_RE.search(d[:4096])
    return m.group(1) if m else None


class ChunkDecoder:
    """Incremental HTTP/1.1 chunked transfer decoding. `done` after the last chunk + trailers."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._state = "size"
        self._need = 0
        self._trailer = 0
        self.done = False

    def feed(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        buf = self._buf
        while not self.done:
            if self._state == "size":
                j = buf.find(b"\r\n")
                if j < 0:
                    if len(buf) > 1024:
                        raise StreamError("bad chunk size line")
                    break
                size_s = bytes(buf[:j]).split(b";", 1)[0].strip()
                try:
                    size = int(size_s, 16)
                except ValueError:
                    raise StreamError(f"bad chunk size {size_s[:20]!r}") from None
                if size < 0:
                    raise StreamError("bad chunk size")
                del buf[:j + 2]
                if size == 0:
                    self._state = "trailer"
                else:
                    self._need, self._state = size, "data"
            elif self._state == "data":
                if not buf:
                    break
                take = min(self._need, len(buf))
                out += buf[:take]
                del buf[:take]
                self._need -= take
                if self._need == 0:
                    self._state = "crlf"
            elif self._state == "crlf":
                if len(buf) < 2:
                    break
                if buf[:2] != b"\r\n":
                    raise StreamError("missing CRLF after chunk")
                del buf[:2]
                self._state = "size"
            else:  # trailer
                j = buf.find(b"\r\n")
                if j < 0:
                    if self._trailer + len(buf) > MAX_TRAILER:
                        raise StreamError("chunked trailer too large")
                    break
                self._trailer += j + 2
                if self._trailer > MAX_TRAILER:
                    raise StreamError("chunked trailer too large")
                del buf[:j + 2]
                if j == 0:
                    self.done = True
        return bytes(out)


def event_target(cwd: str | None, path: str = EVENT_PATH) -> str:
    return f"{path}?{urllib.parse.urlencode({'directory': cwd})}" if cwd else path


# --------------------------------------------------------------------------- probe (doctor)

def probe(port: int, cwd: str | None = None, timeout: float = 1.0, path: str = EVENT_PATH) -> str:
    """Open the SSE stream and wait for its first byte within `timeout`. Returns a short
    description (first event type if one arrived complete); raises oc.ApiDown on failure."""
    deadline = time.monotonic() + timeout
    sock, headers, body = oc.http_open(port, event_target(cwd, path), timeout)
    try:
        ct = headers.get("content-type", "")
        if "text/event-stream" not in ct:
            raise oc.ApiDown(f"content-type {ct or '?'}")
        chunked = "chunked" in headers.get("transfer-encoding", "").lower()
        dec = ChunkDecoder() if chunked else None
        parser = SseParser()
        raw = body
        got_byte = False
        while True:
            if raw:
                got_byte = True
                evs = parser.feed(dec.feed(raw) if dec else raw)
                if evs:
                    return f"first event {event_type(evs[0]) or evs[0].event}"
            rem = deadline - time.monotonic()
            if rem <= 0:
                if got_byte:
                    return "first byte received"
                raise oc.ApiDown("no data within deadline")
            sock.settimeout(rem)
            try:
                raw = sock.recv(65536)
            except TimeoutError:
                if got_byte:
                    return "first byte received"
                raise oc.ApiDown("no data within deadline") from None
            except OSError as e:
                raise oc.ApiDown(str(e) or type(e).__name__) from e
            if not raw:
                raise oc.ApiDown("stream closed")
    except StreamError as e:
        raise oc.ApiDown(str(e)) from e
    finally:
        sock.close()


# --------------------------------------------------------------------------- watcher

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class _Watch:
    """One SSE stream per server port, shared by every agent on it (a TUI with its embedded server
    plus `opencode attach` clients). `members` = {pid: opencode session id | None}; an event about
    session X goes to the members showing X, else (no match / no session) to all of them."""

    def __init__(self, owner: "EventWatcher", port: int, path: str, cwd: str | None,
                 members: dict[int, str | None]) -> None:
        self.owner = owner
        self.port, self.path, self.cwd = port, path, cwd
        self.members = dict(members)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None
        self.healthy = False
        self.gone = False
        self.gone_at = 0.0
        self.connects = 0
        self._last_fire = 0.0
        self._pending: str | None = None
        self._pending_pids: set[int] = set()
        self._due = 0.0
        self.thread = threading.Thread(target=self._run, name=f"agentree-sse-{port}", daemon=True)

    @property
    def target(self) -> tuple[str, str | None]:
        return self.path, self.cwd

    def set_members(self, members: dict[int, str | None]) -> None:
        with self.lock:
            self.members = dict(members)

    def pids(self) -> list[int]:
        with self.lock:
            return list(self.members)

    def _route(self, sid: str | None) -> set[int]:
        with self.lock:
            m = dict(self.members)
        hit = {p for p, s in m.items() if sid is not None and s == sid}
        return hit or set(m)

    def _any_alive(self) -> bool:
        return any(self.owner._pid_alive(p) for p in self.pids())

    # -- control
    def cancel(self) -> None:
        self.stop.set()
        with self.lock:
            s = self.sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- debounce
    def _fire(self, reason: str, pids: set[int]) -> None:
        if self.stop.is_set():
            return
        self._last_fire = time.monotonic()
        self._pending = None
        self._pending_pids = set()
        for pid in sorted(pids):
            try:
                self.owner._on_change(pid, reason)
            except Exception:  # noqa: BLE001 - a broken callback must not kill the watcher
                pass

    def _note(self, reason: str, sid: str | None = None) -> None:
        pids = self._route(sid)
        now = time.monotonic()
        if now - self._last_fire >= self.owner.debounce_s and self._pending is None:
            self._fire(reason, pids)
        else:
            if self._pending is None:
                self._due = self._last_fire + self.owner.debounce_s
            self._pending = reason
            self._pending_pids |= pids

    def _flush(self, force: bool = False) -> None:
        if self._pending is not None and (force or time.monotonic() >= self._due):
            self._fire(self._pending, self._pending_pids)

    # -- thread
    def _run(self) -> None:
        o = self.owner
        lo, hi = o.backoff
        backoff = lo
        refused_since: float | None = None
        while not self.stop.is_set():
            before = self.connects
            try:
                self._session()
                refused_since = None
            except oc.ApiDown as e:
                if isinstance(e.__cause__, ConnectionRefusedError):
                    now = time.monotonic()
                    refused_since = refused_since if refused_since is not None else now
                    if not self._any_alive() or now - refused_since >= o.give_up_s:
                        self.gone, self.gone_at = True, time.monotonic()
                        break
                elif not self._any_alive():
                    self.gone, self.gone_at = True, time.monotonic()
                    break
            except (StreamError, OSError, ValueError):
                pass
            finally:
                self.healthy = False
            if self.stop.is_set():
                break
            if self.connects > before:   # the stream was established, then dropped
                refused_since = None
                backoff = lo
                self._fire("disconnected", set(self.pids()))
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 2, hi)
        self.healthy = False

    def _session(self) -> bool:
        """One connection; returns True if the stream was established (on EOF / stall / stop)."""
        o = self.owner
        sock, headers, body = oc.http_open(self.port, event_target(self.cwd, self.path), o.connect_timeout)
        with self.lock:
            if self.stop.is_set():
                sock.close()
                return False
            self.sock = sock
        try:
            ct = headers.get("content-type", "")
            if "text/event-stream" not in ct:
                raise StreamError(f"content-type {ct or '?'}")
            dec = ChunkDecoder() if "chunked" in headers.get("transfer-encoding", "").lower() else None
            parser = SseParser()
            self.healthy = True
            self.connects += 1
            last_rx = time.monotonic()
            data: bytes | None = body
            while not self.stop.is_set():
                if data:
                    last_rx = time.monotonic()
                    payload = dec.feed(data) if dec else data
                    for ev in parser.feed(payload):
                        if is_status_event(ev):
                            self._note(event_type(ev) or "event", event_session(ev))
                    if dec is not None and dec.done:
                        break
                self._flush()
                now = time.monotonic()
                if now - last_rx > o.stall_s:
                    break
                tick = RECV_TICK
                if self._pending is not None:
                    tick = max(0.005, min(tick, self._due - now))
                sock.settimeout(tick)
                try:
                    data = sock.recv(65536)
                except TimeoutError:
                    data = None
                    continue
                if not data:
                    break
            if not self.stop.is_set():
                self._flush(force=True)
            return True
        finally:
            with self.lock:
                self.sock = None
            try:
                sock.close()
            except OSError:
                pass


class EventWatcher:
    """Watch the SSE streams of a set of opencode agents.

    `update(agents)` reconciles the watch set. There is one stream per server port: agents sharing
    a port (a TUI's embedded server and `opencode attach` clients of it) share it, and events are
    routed to them by session id (`Agent.session_id`, see _Watch). The stream is the server's
    per-directory `/event?directory=<cwd>` when all its agents work in one directory, else
    `/global/event`. An agent without a valid port is skipped; a changed stream target restarts
    the watch, membership changes do not. A watch that gave up (port refused / pids dead) is
    restarted for agents still listed, at most once per `retry_gone_s`.
    `on_change(pid, reason)` runs on the watcher thread (reason = opencode event type, or
    "disconnected" when an established stream drops) — keep it short and thread-safe.
    `healthy()` tells callers which pids need the polling fallback. `close()` is prompt.
    """

    def __init__(self, on_change: Callable[[int, str], None], *, debounce_s: float = DEBOUNCE_S,
                 backoff: tuple[float, float] = BACKOFF, stall_s: float = STALL_S,
                 give_up_s: float = GIVE_UP_S, connect_timeout: float = CONNECT_TIMEOUT,
                 path: str = EVENT_PATH, pid_alive: Callable[[int], bool] | None = None,
                 retry_gone_s: float = RETRY_GONE_S) -> None:
        self._on_change = on_change
        self.debounce_s = debounce_s
        self.backoff = backoff
        self.stall_s = stall_s
        self.give_up_s = give_up_s
        self.retry_gone_s = retry_gone_s
        self.connect_timeout = connect_timeout
        self.path = path
        self._pid_alive = pid_alive or _pid_alive
        self._lock = threading.Lock()
        self._watches: dict[int, _Watch] = {}     # port -> watch
        self._retired: list[_Watch] = []
        self._closed = False

    def _plan(self, agents: Iterable[Agent]) -> dict[int, tuple[str, str | None, dict[int, str | None]]]:
        """{port: (path, cwd, {pid: session id})}."""
        groups: dict[int, tuple[set, dict[int, str | None]]] = {}
        for a in agents:
            port = oc.agent_port(a)
            if port is None:
                continue
            try:
                port, pid = oc.valid_port(port), int(a.pid)
            except (oc.ApiDown, TypeError, ValueError):
                continue
            cwds, members = groups.setdefault(port, (set(), {}))
            cwds.add(a.cwd)
            members[pid] = getattr(a, "session_id", None)
        out = {}
        for port, (cwds, members) in groups.items():
            if len(cwds) == 1:
                out[port] = (self.path, next(iter(cwds)), members)
            else:   # agents of one server in several directories: one stream for all of them
                out[port] = (GLOBAL_EVENT_PATH, None, members)
        return out

    def update(self, agents: Iterable[Agent]) -> None:
        want = self._plan(agents)
        with self._lock:
            if self._closed:
                return
            for port, w in list(self._watches.items()):
                if port not in want or want[port][:2] != w.target:
                    w.cancel()
                    self._retired.append(w)
                    del self._watches[port]
            now = time.monotonic()
            for port, w in list(self._watches.items()):
                # gave up (port refused / pids looked dead) but agents are still listed: retry,
                # at most once per retry_gone_s
                if w.gone and not w.thread.is_alive() and now - w.gone_at >= self.retry_gone_s:
                    del self._watches[port]
            self._retired = [w for w in self._retired if w.thread.is_alive()]
            for port, (path, cwd, members) in want.items():
                w = self._watches.get(port)
                if w is not None:
                    w.set_members(members)
                    continue
                w = _Watch(self, port, path, cwd, members)
                self._watches[port] = w
                w.thread.start()

    def healthy(self) -> dict[int, bool]:
        """{pid: its server's stream connected} for every watched pid (attach clients share their
        server's stream); a watch that gave up or whose thread ended is unhealthy. Agents never
        watched (no / invalid port) are simply absent: callers compare against their full agent
        set (see OpencodeProvider.refresh_policy)."""
        with self._lock:
            ws = list(self._watches.values())
        out: dict[int, bool] = {}
        for w in ws:
            ok = bool(w.healthy and w.thread.is_alive() and not w.gone)
            for pid in w.pids():
                out[pid] = ok
        return out

    def watching(self) -> dict[int, int]:
        """{pid: port} currently in the watch set (including watchers that gave up)."""
        with self._lock:
            ws = list(self._watches.values())
        return {pid: w.port for w in ws for pid in w.pids()}

    def streams(self) -> dict[int, list[int]]:
        """{port: [pids]}: one stream per port."""
        with self._lock:
            ws = list(self._watches.values())
        return {w.port: sorted(w.pids()) for w in ws}

    def close(self, timeout: float = 0.5) -> None:
        with self._lock:
            self._closed = True
            ws = list(self._watches.values()) + self._retired
            self._watches.clear()
            self._retired = []
        for w in ws:
            w.cancel()
        deadline = time.monotonic() + timeout
        for w in ws:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            w.thread.join(rem)

    def __enter__(self) -> "EventWatcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
