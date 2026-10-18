import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agentree.providers import _opencode_events as ev
from agentree.providers import opencode as oc
from agentree.types import Agent

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live", "opencode_events.txt")


def fixture_bytes() -> bytes:
    with open(FIX, "rb") as fh:
        return fh.read()


def types(events):
    return [ev.event_type(e) for e in events]


def frame(typ: str, **props) -> bytes:
    import json
    return b"data: " + json.dumps({"id": "evt_x", "type": typ, "properties": props}).encode() + b"\n\n"


def agent(pid: int, port: int | None, cwd: str | None = "/w", sid: str | None = None) -> Agent:
    return oc.make_agent(pid=pid, cwd=cwd, port=port, session_name=None, pane_id=None, start_time=None,
                         session_id=sid)


def wait_for(pred, timeout=3.0, step=0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


# --------------------------------------------------------------------------- pure parser

class ParseSseTest(unittest.TestCase):
    def test_live_fixture(self):
        evs = ev.parse_sse([fixture_bytes()])
        t = types(evs)
        self.assertEqual(t[0], "server.connected")
        self.assertIn("server.heartbeat", t)
        self.assertIn("session.status", t)
        self.assertIn("message.part.delta", t)
        self.assertTrue(all(e.event == "message" for e in evs))
        status = [e for e in evs if ev.is_status_event(e)]
        self.assertEqual(types(status), ["server.connected", "session.updated", "session.status"])

    def test_fixture_comment_counted(self):
        p = ev.SseParser()
        p.feed(fixture_bytes())
        self.assertGreaterEqual(p.comments, 1)

    def test_split_anywhere_and_line_endings(self):
        data = fixture_bytes()
        whole = ev.parse_sse([data])
        self.assertEqual(ev.parse_sse([data[i:i + 1] for i in range(len(data))]), whole)
        self.assertEqual(ev.parse_sse([data[i:i + 7] for i in range(0, len(data), 7)]), whole)
        crlf = data.replace(b"\n", b"\r\n")
        self.assertEqual(ev.parse_sse([crlf[i:i + 1] for i in range(len(crlf))]), whole)
        cr = data.replace(b"\n", b"\r")
        self.assertEqual(ev.parse_sse([cr[i:i + 3] for i in range(0, len(cr), 3)]), whole)

    def test_fields(self):
        evs = ev.parse_sse([
            "\ufeff: heartbeat\n\n",
            "event: custom.thing\nid: 7\ndata: line1\ndata:line2\ndata\n\n",
            "retry: 100\nunknown: x\n\n",            # no data -> nothing dispatched
            "data: {\"type\":\"session.idle\"}\n\n",
            "data: trailing without terminator\n",
        ])
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0], ev.SseEvent("custom.thing", "line1\nline2\n", "7"))
        self.assertEqual(ev.event_type(evs[0]), "custom.thing")
        self.assertEqual(evs[1].event, "message")
        self.assertEqual(evs[1].id, "7")     # last event id persists
        self.assertEqual(ev.event_type(evs[1]), "session.idle")

    def test_global_payload_and_bad_json(self):
        g = ev.parse_sse([b'data: {"payload":{"id":"evt_1","type":"session.status","properties":{}}}\n\n'])
        self.assertEqual(ev.event_type(g[0]), "session.status")
        self.assertTrue(ev.is_status_event(g[0]))
        bad = ev.SseEvent("message", '{"id":"x","type":"permission.asked", truncated')
        self.assertEqual(ev.event_type(bad), "permission.asked")
        self.assertIsNone(ev.event_type(ev.SseEvent("message", "not json")))
        self.assertFalse(ev.is_status_event(ev.SseEvent("message", "not json")))

    def test_is_status_event(self):
        def mk(t):
            return ev.SseEvent("message", '{"type":"%s","properties":{}}' % t)
        for t in ("session.status", "session.idle", "session.updated", "session.created", "session.deleted",
                  "session.error", "permission.asked", "permission.replied", "permission.v2.asked",
                  "question.asked", "question.v2.rejected", "server.connected", "server.instance.disposed"):
            self.assertTrue(ev.is_status_event(mk(t)), t)
        for t in ("server.heartbeat", "message.part.delta", "message.part.updated", "message.updated",
                  "session.diff", "session.next.text.delta", "file.edited", "lsp.updated"):
            self.assertFalse(ev.is_status_event(mk(t)), t)

    def test_oversized_event_dropped(self):
        p = ev.SseParser(max_event=100)
        out = p.feed(b"data: " + b"x" * 300 + b"\n\n" + frame("session.idle"))
        self.assertEqual(types(out), ["session.idle"])
        p = ev.SseParser(max_event=100)
        out = p.feed(b"data: " + b"y" * 150)          # unterminated monster line
        out += p.feed(b"y" * 50 + b"\n\n" + frame("session.status"))
        self.assertEqual(types(out), ["session.status"])

    def test_large_payload_type_sniffed(self):
        big = '{"id":"evt_1","type":"session.updated","properties":{"x":"' + "z" * (ev.JSON_MAX + 10) + '"}}'
        self.assertEqual(ev.event_type(ev.SseEvent("message", big)), "session.updated")


class ChunkDecoderTest(unittest.TestCase):
    @staticmethod
    def chunked(parts: list[bytes]) -> bytes:
        return b"".join(b"%x;ext=1\r\n" % len(p) + p + b"\r\n" for p in parts) + b"0\r\nX-T: 1\r\n\r\n"

    def test_roundtrip_bytewise(self):
        data = fixture_bytes()
        enc = self.chunked([data[i:i + 59] for i in range(0, len(data), 59)])
        d = ev.ChunkDecoder()
        out = b"".join(d.feed(enc[i:i + 1]) for i in range(len(enc)))
        self.assertEqual(out, data)
        self.assertTrue(d.done)

    def test_trailer_bounded(self):
        d = ev.ChunkDecoder()
        d.feed(b"3\r\nabc\r\n0\r\n")
        with self.assertRaises(ev.StreamError):
            for _ in range(100):
                d.feed(b"X-Trailer: " + b"y" * 200)       # one endless trailer line
        d = ev.ChunkDecoder()
        with self.assertRaises(ev.StreamError):
            d.feed(b"0\r\n" + b"T: v\r\n" * 2000)       # many trailer lines
        d = ev.ChunkDecoder()
        self.assertEqual(d.feed(b"0\r\nT: v\r\n\r\n"), b"")
        self.assertTrue(d.done)

    def test_bad_encoding(self):
        with self.assertRaises(ev.StreamError):
            ev.ChunkDecoder().feed(b"zz\r\nabc")
        with self.assertRaises(ev.StreamError):
            ev.ChunkDecoder().feed(b"3\r\nabcXY")


# --------------------------------------------------------------------------- live local server

class _Srv:
    """Local SSE server. `script(handler, n)` drives connection n (0-based)."""

    def __init__(self, script, status=200, ctype="text/event-stream", port=0):
        self.stop = threading.Event()
        self.paths: list[str] = []
        self.accepts: list[str] = []
        self.n = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def chunk(self, data: bytes) -> None:
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()

            def hold(self, secs: float = 30.0) -> None:
                outer.stop.wait(secs)

            def do_GET(self):
                outer.paths.append(self.path)
                outer.accepts.append(self.headers.get("Accept", ""))
                n = outer.n
                outer.n += 1
                self.send_response(status)
                if status in (301, 302):
                    self.send_header("Location", "http://example.com/event")
                self.send_header("Content-Type", ctype)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    script(self, n)
                except OSError:
                    pass
                self.close_connection = True

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[float, int, str]] = []
        self.lock = threading.Lock()
        self.servers: list[_Srv] = []
        self.watchers: list[ev.EventWatcher] = []

    def tearDown(self):
        for w in self.watchers:
            w.close()
        for s in self.servers:
            s.close()

    def on_change(self, pid, reason):
        with self.lock:
            self.calls.append((time.monotonic(), pid, reason))

    def reasons(self, pid=None):
        with self.lock:
            return [r for _, p, r in self.calls if pid is None or p == pid]

    def srv(self, script, **kw) -> _Srv:
        s = _Srv(script, **kw)
        self.servers.append(s)
        return s

    def watcher(self, **kw) -> ev.EventWatcher:
        kw.setdefault("debounce_s", 0.05)
        kw.setdefault("backoff", (0.05, 0.2))
        w = ev.EventWatcher(self.on_change, **kw)
        self.watchers.append(w)
        return w

    def test_events_heartbeats_split_frames(self):
        def script(h, n):
            h.chunk(frame("server.connected"))
            h.chunk(b": keepalive\n\n")
            h.chunk(frame("server.heartbeat"))
            h.chunk(frame("message.part.delta", delta="hi"))
            f = frame("session.status", sessionID="ses_1", status={"type": "busy"})
            h.chunk(f[:17])
            time.sleep(0.1)
            h.chunk(f[17:])
            h.hold()
        s = self.srv(script)
        w = self.watcher()
        w.update([agent(4242, s.port, "/my dir")])
        self.assertTrue(wait_for(lambda: self.reasons() == ["server.connected", "session.status"]), self.reasons())
        self.assertEqual(w.healthy(), {4242: True})
        self.assertEqual(s.paths[0], "/event?directory=%2Fmy+dir")
        self.assertEqual(s.accepts[0], "text/event-stream")
        time.sleep(0.2)
        self.assertEqual(self.reasons(), ["server.connected", "session.status"])

    def test_debounce(self):
        def script(h, n):
            for _ in range(30):
                h.chunk(frame("session.updated"))
            time.sleep(0.05)
            h.chunk(frame("session.idle"))
            h.hold()
        s = self.srv(script)
        w = self.watcher(debounce_s=0.2)
        w.update([agent(1, s.port)])
        self.assertTrue(wait_for(lambda: "session.idle" in self.reasons()))
        time.sleep(0.3)
        with self.lock:
            ts = [t for t, _, _ in self.calls]
        self.assertLessEqual(len(ts), 3)
        self.assertEqual(self.reasons()[-1], "session.idle")
        for a, b in zip(ts, ts[1:]):
            self.assertGreaterEqual(b - a, 0.19)

    def test_reconnect_after_server_close(self):
        def script(h, n):
            h.chunk(frame("server.connected"))
            if n == 0:
                h.wfile.write(b"0\r\n\r\n")   # end of stream
                h.wfile.flush()
                return
            h.chunk(frame("session.status", status={"type": "idle"}))
            h.hold()
        s = self.srv(script)
        w = self.watcher(debounce_s=0.001)    # events further apart than the debounce are not coalesced
        w.update([agent(7, s.port)])
        self.assertTrue(wait_for(lambda: "session.status" in self.reasons()), self.reasons())
        self.assertEqual(s.n, 2)
        self.assertEqual(w._watches[s.port].connects, 2)
        self.assertEqual(self.reasons()[:2], ["server.connected", "disconnected"])
        self.assertEqual(self.reasons()[-1], "session.status")
        self.assertTrue(wait_for(lambda: w.healthy() == {7: True}))

    def test_reconnect_when_server_restarts_on_same_port(self):
        s = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()))
        port = s.port
        w = self.watcher()
        w.update([agent(8, port)])
        self.assertTrue(wait_for(lambda: w.healthy() == {8: True}))
        s.close()
        self.servers.remove(s)
        self.assertTrue(wait_for(lambda: w.healthy() == {8: False}))
        # a new server on the same port (HTTPServer sets SO_REUSEADDR)
        try:
            s2 = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()), port=port)
        except OSError:
            self.skipTest("port reuse not possible")
        self.assertEqual(s2.port, port)
        self.assertTrue(wait_for(lambda: w.healthy() == {8: True}, timeout=3))
        self.assertGreaterEqual(self.reasons(8).count("server.connected"), 2)

    def test_update_removes_and_restarts(self):
        s = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()))
        s2 = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()))
        w = self.watcher()
        w.update([agent(1, s.port), agent(2, s2.port, "/other"), agent(3, None)])
        self.assertEqual(sorted(w.watching()), [1, 2])
        self.assertTrue(wait_for(lambda: w.healthy() == {1: True, 2: True}))
        t1 = w._watches[s.port].thread
        t2 = w._watches[s2.port].thread
        w.update([agent(2, s2.port, "/other")])
        self.assertTrue(wait_for(lambda: not t1.is_alive(), timeout=1.0))
        self.assertTrue(t2.is_alive())
        self.assertEqual(w.watching(), {2: s2.port})
        # cwd change restarts the watcher for that port
        w.update([agent(2, s2.port, "/moved")])
        self.assertTrue(wait_for(lambda: not t2.is_alive(), timeout=1.0))
        self.assertTrue(wait_for(lambda: w.healthy() == {2: True}))
        self.assertIn("/event?directory=%2Fmoved", s2.paths)

    def test_shared_port_one_stream_routed_by_session(self):
        # a TUI with its embedded server (pid 1) + two `opencode attach` clients of it (2, 3)
        go = threading.Event()

        def script(h, n):
            h.chunk(frame("server.connected"))
            go.wait(3)
            h.chunk(frame("session.status", sessionID="ses_B", status={"type": "busy"}))
            time.sleep(0.15)
            h.chunk(frame("session.updated", info={"id": "ses_C", "title": "t"}))
            time.sleep(0.15)
            h.chunk(frame("session.status", sessionID="ses_child", status={"type": "busy"}))
            h.hold()
        s = self.srv(script)
        w = self.watcher(debounce_s=0.05)
        w.update([agent(1, s.port, "/w", "ses_A"), agent(2, s.port, "/w", "ses_B"), agent(3, s.port, "/w", "ses_C")])
        self.assertEqual(w.streams(), {s.port: [1, 2, 3]})
        self.assertTrue(wait_for(lambda: w.healthy() == {1: True, 2: True, 3: True}))
        self.assertEqual(s.n, 1)                                        # one connection for the port
        self.assertTrue(wait_for(lambda: sorted(self.reasons()) == ["server.connected"] * 3))
        go.set()
        # ses_B -> pid 2 only; ses_C (session.updated info.id) -> pid 3; unknown session -> all
        self.assertTrue(wait_for(lambda: len(self.reasons()) == 3 + 1 + 1 + 3), self.reasons())
        time.sleep(0.1)
        self.assertEqual(self.reasons(1), ["server.connected", "session.status"])
        self.assertEqual(self.reasons(2), ["server.connected", "session.status", "session.status"])
        self.assertEqual(self.reasons(3), ["server.connected", "session.updated", "session.status"])
        # membership change (an attach client quits) keeps the stream
        th = w._watches[s.port].thread
        w.update([agent(1, s.port, "/w", "ses_A"), agent(2, s.port, "/w", "ses_B")])
        self.assertIs(w._watches[s.port].thread, th)
        self.assertEqual(w.healthy(), {1: True, 2: True})
        self.assertEqual(s.n, 1)

    def test_shared_port_in_several_dirs_uses_global_stream(self):
        s = self.srv(lambda h, n: (h.chunk(b'data: {"payload":{"type":"server.connected"}}\n\n'), h.hold()))
        w = self.watcher()
        w.update([agent(1, s.port, "/w"), agent(2, s.port, "/v", "ses_x")])
        self.assertTrue(wait_for(lambda: w.healthy() == {1: True, 2: True}))
        self.assertEqual(s.paths, ["/global/event"])
        self.assertTrue(wait_for(lambda: sorted(self.reasons()) == ["server.connected"] * 2))

    def test_event_session(self):
        cases = [
            (frame("session.status", sessionID="ses_1", status={"type": "busy"}), "ses_1"),
            (frame("session.updated", info={"id": "ses_2"}), "ses_2"),
            (frame("message.updated", info={"id": "msg_1", "sessionID": "ses_3"}), "ses_3"),
            (frame("server.connected"), None),
            (b'data: {"directory":"/w","payload":{"type":"permission.asked","properties":{"sessionID":"ses_4"}}}\n\n', "ses_4"),
            (b"data: not json\n\n", None),
        ]
        for raw, want in cases:
            with self.subTest(raw=raw[:60]):
                self.assertEqual(ev.event_session(ev.parse_sse([raw])[0]), want)
        big = 'data: {"type":"session.status","properties":{"sessionID":"ses_5","x":"' + "y" * (ev.JSON_MAX + 10) + '"}}\n\n'
        self.assertEqual(ev.event_session(ev.parse_sse([big])[0]), "ses_5")

    def test_close_is_fast(self):
        s = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()))
        w = self.watcher()
        others = [self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold())) for _ in range(3)]
        w.update([agent(i, x.port) for i, x in enumerate([s, *others], 1)])
        self.assertTrue(wait_for(lambda: all(w.healthy().values()) and len(w.healthy()) == 4))
        threads = [x.thread for x in w._watches.values()]
        t0 = time.monotonic()
        w.close()
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertTrue(all(t.daemon for t in threads))
        n = len(self.reasons())
        w.update([agent(9, s.port)])          # closed: ignored
        self.assertEqual(w.watching(), {})
        time.sleep(0.1)
        self.assertEqual(len(self.reasons()), n)

    def test_close_during_backoff(self):
        w = self.watcher(backoff=(5.0, 5.0), give_up_s=60)
        port = free_port()
        w.update([agent(os.getpid(), port)])
        th = w._watches[port].thread
        time.sleep(0.1)
        t0 = time.monotonic()
        w.close()
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertFalse(th.is_alive())

    def test_gives_up_when_port_gone(self):
        w = self.watcher(pid_alive=lambda pid: False)
        port = free_port()
        w.update([agent(11, port)])
        th = w._watches[port].thread
        self.assertTrue(wait_for(lambda: not th.is_alive(), timeout=1.0))
        self.assertTrue(w._watches[port].gone)
        self.assertEqual(w.healthy(), {11: False})
        w.update([agent(11, port)])                  # same key: not restarted
        self.assertIs(w._watches[port].thread, th)
        self.assertEqual(self.reasons(), [])

    def test_given_up_watch_restarted_after_retry_interval(self):
        alive = {"v": False}
        w = self.watcher(pid_alive=lambda pid: alive["v"], retry_gone_s=0.3)
        port = free_port()
        w.update([agent(12, port)])
        th = w._watches[port].thread
        self.assertTrue(wait_for(lambda: not th.is_alive(), timeout=1.0))
        self.assertEqual(w.healthy(), {12: False})
        w.update([agent(12, port)])                       # too soon: same (given-up) watch
        self.assertIs(w._watches[port].thread, th)
        # the agent's server comes up on that port; after retry_gone_s the next update retries
        s = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()), port=port)
        alive["v"] = True
        time.sleep(0.35)
        w.update([agent(12, port)])
        self.assertIsNot(w._watches[port].thread, th)
        self.assertTrue(wait_for(lambda: w.healthy() == {12: True}))
        self.assertGreaterEqual(s.n, 1)
        w.update([])                                       # gone from the set: dropped, never retried
        self.assertEqual(w.watching(), {})

    def test_healthy_false_for_stopped_watch(self):
        s = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()))
        w = self.watcher()
        w.update([agent(1, s.port)])
        self.assertTrue(wait_for(lambda: w.healthy() == {1: True}))
        x = w._watches[s.port]
        x.cancel()                                         # thread ends while still mapped
        self.assertTrue(wait_for(lambda: not x.thread.is_alive()))
        self.assertEqual(w.healthy(), {1: False})

    def test_gives_up_after_refused_for_give_up_s(self):
        w = self.watcher(give_up_s=0.3)
        port = free_port()
        w.update([agent(os.getpid(), port)])
        th = w._watches[port].thread
        self.assertTrue(wait_for(lambda: not th.is_alive(), timeout=2.0))

    def test_redirect_and_wrong_content_type_unhealthy(self):
        s1 = self.srv(lambda h, n: h.hold(0.2), status=302)
        s2 = self.srv(lambda h, n: (h.chunk(b"{}"), h.hold()), ctype="application/json")
        w = self.watcher(give_up_s=60)
        w.update([agent(1, s1.port), agent(2, s2.port)])
        self.assertTrue(wait_for(lambda: s1.n >= 2 and s2.n >= 2))
        self.assertEqual(w.healthy(), {1: False, 2: False})
        self.assertEqual(self.reasons(), [])

    def test_callback_exception_does_not_kill_watcher(self):
        calls = []

        def boom(pid, reason):
            calls.append(reason)
            raise RuntimeError("x")

        def script(h, n):
            h.chunk(frame("server.connected"))
            time.sleep(0.1)
            h.chunk(frame("session.idle"))
            h.hold()
        s = self.srv(script)
        w = ev.EventWatcher(boom, debounce_s=0.01)
        self.watchers.append(w)
        w.update([agent(1, s.port)])
        self.assertTrue(wait_for(lambda: calls == ["server.connected", "session.idle"]), calls)
        self.assertEqual(w.healthy(), {1: True})

    def test_probe(self):
        s = self.srv(lambda h, n: (h.chunk(frame("server.connected")), h.hold()))
        self.assertEqual(ev.probe(s.port, "/w", 1.0), "first event server.connected")
        silent = self.srv(lambda h, n: h.hold())
        t0 = time.monotonic()
        with self.assertRaises(oc.ApiDown):
            ev.probe(silent.port, None, 0.3)
        self.assertLess(time.monotonic() - t0, 0.6)
        notfound = self.srv(lambda h, n: None, status=404)
        with self.assertRaises(oc.ApiDown):
            ev.probe(notfound.port, None, 0.5)


class PlanTest(unittest.TestCase):
    def test_plan_uses_meta_port_and_session_id(self):
        # the watcher only ever receives opencode agents (LiveStatus routes by provider); agents
        # without a (valid) meta port are skipped
        w = ev.EventWatcher(lambda pid, reason: None)
        try:
            a = agent(1, 5000, "/w", "ses_1")
            no_port = Agent(pid=2, cwd="/w", session_name=None, pane_id=None)          # meta {}
            bad = agent(3, 70000)
            plan = w._plan([a, no_port, bad])
            self.assertEqual(sorted(plan), [5000])
            self.assertEqual(plan[5000][2], {1: "ses_1"})
            self.assertEqual(w._plan([no_port, bad]), {})
        finally:
            w.close()


class LocalhostOnlyTest(unittest.TestCase):
    def test_http_open_only_connects_to_loopback(self):
        seen = []

        def fake_conn(addr, timeout=None):
            seen.append(addr)
            raise ConnectionRefusedError("nope")
        with mock.patch.object(oc.socket, "create_connection", fake_conn):
            with self.assertRaises(oc.ApiDown) as cm:
                oc.http_open(5000, "/event", 0.2)
        self.assertEqual(seen, [("127.0.0.1", 5000)])
        self.assertIsInstance(cm.exception.__cause__, ConnectionRefusedError)

    def test_bad_ports_and_targets_rejected(self):
        for p in (0, -1, 70000, "x", None, "5000; host"):
            with self.assertRaises(oc.ApiDown):
                oc.http_open(p, "/event", 0.1)  # type: ignore[arg-type]
        with self.assertRaises(oc.ApiDown):
            oc.http_open(5000, "http://evil.example/event", 0.1)
        with self.assertRaises(oc.ApiDown):
            oc.http_open(5000, "@evil.example/event", 0.1)

    def test_watcher_api_is_port_only(self):
        import inspect
        params = inspect.signature(ev.EventWatcher.__init__).parameters
        self.assertNotIn("host", params)
        seen = []

        def fake_conn(addr, timeout=None):
            seen.append(addr)
            raise ConnectionRefusedError("nope")
        w = ev.EventWatcher(lambda p, r: None, backoff=(0.01, 0.01), pid_alive=lambda p: False)
        with mock.patch.object(oc.socket, "create_connection", fake_conn):
            w.update([agent(1, 5999), agent(2, "bad")])  # type: ignore[arg-type]
            wait_for(lambda: bool(seen))
            w.close()
        self.assertEqual(seen, [("127.0.0.1", 5999)])
        self.assertEqual(w.watching(), {})


if __name__ == "__main__":
    unittest.main()
