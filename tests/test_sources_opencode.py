import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

from agentree.providers import opencode as oc

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")


def fx_json(name: str):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


NOW = 1_800_000_000_000


class MapStatusTest(unittest.TestCase):
    def ms(self, **kw):
        base = dict(api_ok=True, pending_input=False, session_status=None, last_activity_ms=None,
                    busy_since_ms=None, now_ms=NOW)
        base.update(kw)
        return oc.map_status(**base)

    def test_unknown_when_api_down(self):
        self.assertEqual(self.ms(api_ok=False, pending_input=True, session_status="busy"), ("unknown", None))

    def test_needs_input_beats_busy(self):
        self.assertEqual(self.ms(pending_input=True, session_status="busy", last_activity_ms=NOW - 5000),
                         ("needs_input", 5))

    def test_busy_and_retry(self):
        self.assertEqual(self.ms(session_status="busy", busy_since_ms=NOW - 120_000, last_activity_ms=NOW),
                         ("busy", 120))
        self.assertEqual(self.ms(session_status="retry", last_activity_ms=NOW - 3000), ("busy", 3))

    def test_idle_then_done(self):
        self.assertEqual(self.ms(session_status="idle", last_activity_ms=NOW - 60_000), ("idle", 60))
        self.assertEqual(self.ms(last_activity_ms=NOW - 31 * 60_000), ("done", 31 * 60))
        self.assertEqual(self.ms(last_activity_ms=NOW - 1800_000), ("done", 1800))

    def test_no_session(self):
        self.assertEqual(self.ms(), ("idle", None))

    def test_future_timestamp_clamped(self):
        self.assertEqual(self.ms(last_activity_ms=NOW + 10_000), ("idle", 0))


class HelpersTest(unittest.TestCase):
    def test_port_from_argv(self):
        self.assertEqual(oc.port_from_argv(["opencode", "--port", "5004", "--auto"]), 5004)
        self.assertEqual(oc.port_from_argv(["opencode", "--port=5001"]), 5001)
        self.assertIsNone(oc.port_from_argv(["opencode", "--port", "0"]))
        self.assertIsNone(oc.port_from_argv(["opencode"]))
        self.assertIsNone(oc.port_from_argv(["opencode", "--port"]))

    def test_pick_session_live(self):
        sessions = fx_json("opencode_session.json")
        cwd = sessions[0]["directory"]
        s = oc.pick_session(sessions, cwd, {})
        best = max((x for x in sessions if x["directory"] == cwd and not x.get("parentID")),
                   key=lambda x: x["time"]["updated"])
        self.assertEqual(s["id"], best["id"])
        self.assertIsNone(oc.pick_session(sessions, "/nonexistent/dir", {}))

    def test_pick_session_prefers_busy_and_filters_children(self):
        sessions = [
            {"id": "ses_a", "directory": "/w", "time": {"updated": 100}},
            {"id": "ses_b", "directory": "/w", "time": {"updated": 50}},
            {"id": "ses_c", "directory": "/w", "parentID": "ses_a", "time": {"updated": 999}},
            {"id": "ses_d", "directory": "/other", "time": {"updated": 500}},
        ]
        self.assertEqual(oc.pick_session(sessions, "/w", {})["id"], "ses_a")
        self.assertEqual(oc.pick_session(sessions, "/w", {"ses_b": {"type": "busy"}})["id"], "ses_b")
        self.assertEqual(oc.pick_session(sessions, None, {})["id"], "ses_d")

    def test_summarize_live_messages(self):
        msgs = fx_json("opencode_messages.json")
        s = oc.summarize_messages(msgs)
        self.assertIsNotNone(s["last_activity_ms"])
        assistant_with_tokens = [m for m in msgs if m["info"]["role"] == "assistant" and oc.message_tokens(m["info"])]
        if assistant_with_tokens:
            self.assertEqual(s["context_tokens"], oc.message_tokens(assistant_with_tokens[-1]["info"]))
            self.assertEqual(s["model"], assistant_with_tokens[-1]["info"]["modelID"])
        texts = [p["text"] for m in msgs if m["info"]["role"] == "assistant" for p in m["parts"]
                 if p["type"] == "text" and p.get("text", "").strip()]
        if texts:
            self.assertTrue(s["last_message"])

    def test_summarize_synthetic(self):
        msgs = [
            {"info": {"role": "user", "time": {"created": 1000}}, "parts": [{"type": "text", "text": "do it"}]},
            {"info": {"role": "assistant", "time": {"created": 1100, "completed": 1200}, "providerID": "p",
                      "modelID": "m", "tokens": {"input": 10, "output": 5, "reasoning": 1, "cache": {"read": 100, "write": 4}}},
             "parts": [{"type": "text", "text": "  final answer  "}, {"type": "text", "text": "x", "synthetic": True}]},
            {"info": {"role": "assistant", "time": {"created": 1300}, "providerID": "p", "modelID": "m",
                      "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}},
             "parts": [{"type": "tool"}]},
        ]
        s = oc.summarize_messages(msgs)
        self.assertEqual(s["last_message"], "final answer")
        self.assertEqual(s["context_tokens"], 120)
        self.assertEqual((s["provider"], s["model"]), ("p", "m"))
        self.assertEqual(s["last_activity_ms"], 1300)
        self.assertEqual(s["last_user_ms"], 1000)

    def test_message_tokens_total(self):
        self.assertEqual(oc.message_tokens({"tokens": {"total": 7, "input": 1}}), 7)
        self.assertIsNone(oc.message_tokens({}))

    def test_providers_limits_live(self):
        limits = oc.parse_providers_limits(fx_json("opencode_config_providers.json"))
        self.assertTrue(limits)
        for (p, m), v in limits.items():
            self.assertIsInstance(v, int)
            self.assertGreater(v, 0)


def fx_pane_title(session: str, command_has: str = "opencode"):
    panes = fx_json(f"zellij_{session}_list_panes.json")
    return next(p["title"] for p in panes if not p.get("is_plugin") and command_has in (p.get("pane_command") or ""))


class PaneTitleTest(unittest.TestCase):
    SESS = [
        {"id": "ses_a", "directory": "/w", "title": "Refactor token refresh", "time": {"updated": 300}},
        {"id": "ses_b", "directory": "/w", "title": "Write refresh tests", "time": {"updated": 200}},
        {"id": "ses_c", "directory": "/w", "title": "Refactor token refresh flow", "time": {"updated": 100}},
        {"id": "ses_d", "directory": "/w/other", "title": "Elsewhere in project", "time": {"updated": 50}},
        {"id": "ses_k", "directory": "/w", "parentID": "ses_a", "title": "Write refresh tests", "time": {"updated": 999}},
    ]

    def test_parse(self):
        self.assertEqual(oc.parse_pane_title("OpenCode"), ("home", None))
        self.assertEqual(oc.parse_pane_title("OC | Fix the thing"), ("session", "Fix the thing"))
        self.assertEqual(oc.parse_pane_title("OC | Fix the th..."), ("session", "Fix the th..."))
        self.assertIsNone(oc.parse_pane_title(None))
        self.assertIsNone(oc.parse_pane_title(""))
        self.assertIsNone(oc.parse_pane_title("OC | "))
        self.assertIsNone(oc.parse_pane_title("~/P/web-app"))

    def test_live_fixture_panes_are_home_screen(self):
        sessions = fx_json("opencode_session.json")
        cwd = sessions[0]["directory"]
        for zs in ("web-app", "dev"):
            title = fx_pane_title(zs)
            self.assertEqual(title, "OpenCode")
            self.assertEqual(oc.select_session(sessions, cwd, {}, title), (None, None))
        # without a pane title: the old behaviour (most recent session in cwd)
        s, t = oc.select_session(sessions, cwd, {}, None)
        self.assertEqual(s["id"], oc.pick_session(sessions, cwd, {})["id"])
        self.assertEqual(t, s["title"])

    def test_live_fixture_truncated_title(self):
        sessions = fx_json("opencode_session.json")
        cwd = sessions[0]["directory"]
        target = sessions[2]                       # not the most recent one
        for trunc in ("...", "…"):
            s, t = oc.select_session(sessions, cwd, {}, "OC | " + target["title"][:20] + trunc)
            self.assertEqual(s["id"], target["id"])
            self.assertEqual(t, target["title"])

    def test_two_agents_same_dir_different_titles(self):
        a, ta = oc.select_session(self.SESS, "/w", {}, "OC | Refactor token refresh")
        b, tb = oc.select_session(self.SESS, "/w", {}, "OC | Write refresh tests")
        self.assertEqual((a["id"], ta), ("ses_a", "Refactor token refresh"))     # exact beats prefix (ses_c)
        self.assertEqual((b["id"], tb), ("ses_b", "Write refresh tests"))        # root, not the child ses_k

    def test_truncated_prefix(self):
        s, _ = oc.select_session(self.SESS, "/w", {}, "OC | Refactor token refresh f…")
        self.assertEqual(s["id"], "ses_c")
        s, _ = oc.select_session(self.SESS, "/w", {}, "OC | Write refr...")
        self.assertEqual(s["id"], "ses_b")
        # ambiguous prefix: busy (this process) first, then most recent
        s, _ = oc.select_session(self.SESS, "/w", {}, "OC | Refactor tok...")
        self.assertEqual(s["id"], "ses_a")
        s, _ = oc.select_session(self.SESS, "/w", {"ses_c": {"type": "busy"}}, "OC | Refactor tok...")
        self.assertEqual(s["id"], "ses_c")

    def test_other_directory_in_project_and_no_match(self):
        s, t = oc.select_session(self.SESS, "/w", {}, "OC | Elsewhere in project")
        self.assertEqual((s["id"], t), ("ses_d", "Elsewhere in project"))
        self.assertEqual(oc.select_session(self.SESS, "/w", {}, "OC | Not listed"), (None, "Not listed"))

    def test_home_screen_ignores_busy(self):
        self.assertEqual(oc.select_session(self.SESS, "/w", {"ses_b": {"type": "busy"}}, "OpenCode"), (None, None))

    def test_no_pane_title_prefers_busy(self):
        s, _ = oc.select_session(self.SESS, "/w", {"ses_b": {"type": "busy"}}, None)
        self.assertEqual(s["id"], "ses_b")
        s, _ = oc.select_session(self.SESS, "/w", {"ses_b": {"type": "busy"}}, "fish")
        self.assertEqual(s["id"], "ses_b")


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        self.server.seen.append((u.path, parse_qs(u.query)))
        body = self.routes.get(u.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class AgentHttpTest(unittest.TestCase):
    def serve(self, routes):
        handler = type("H", (_Handler,), {"routes": routes})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.seen = []
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def test_agent_needs_input(self):
        oc._limits_cache.clear()
        routes = {
            "/session": [{"id": "ses_1", "directory": "/w", "title": "T", "time": {"updated": NOW}}],
            "/session/status": {"ses_1": {"type": "busy"}},
            "/permission": [{"id": "per_1", "sessionID": "ses_1"}],
            "/question": [],
            "/session/ses_1/message": [{"info": {"role": "assistant", "providerID": "p", "modelID": "m",
                                                 "time": {"created": NOW}, "tokens": {"total": 50}},
                                        "parts": [{"type": "text", "text": "hi"}]}],
            "/config/providers": {"providers": [{"id": "p", "models": {"m": {"limit": {"context": 1000}}}}]},
        }
        srv = self.serve(routes)
        port = srv.server_address[1]
        a, warn = oc._agent(42, ("opencode", "--port", str(port)), {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "3"},
                            "/w", port, 2.0)
        self.assertIsNone(warn)
        self.assertTrue(a.meta["api_ok"])
        self.assertEqual((a.provider, a.meta, a.status_quality, a.status_note),
                         ("opencode", {"port": port, "api_ok": True}, "exact", None))
        self.assertEqual(a.status, "needs_input")
        self.assertEqual((a.session_name, a.pane_id), ("s", 3))
        self.assertEqual((a.title, a.session_id, a.last_message), ("T", "ses_1", "hi"))
        self.assertEqual((a.context_tokens, a.context_limit), (50, 1000))
        # every call is scoped to the process cwd
        for path, q in srv.seen:
            self.assertEqual(q.get("directory"), ["/w"], path)

    def test_agent_api_down(self):
        a, warn = oc._agent(1, ("opencode",), {}, "/w", 1, 0.5)  # port 1: refused
        self.assertFalse(a.meta["api_ok"])
        self.assertEqual(a.status, "unknown")
        self.assertEqual((a.provider, a.meta, a.status_quality, a.status_note),
                         ("opencode", {"port": 1, "api_ok": False}, "unavailable", "Live status unavailable"))
        self.assertIsNotNone(warn)

    def test_agent_no_port(self):
        a, warn = oc._agent(1, ("opencode",), {"ZELLIJ_PANE_ID": "x"}, None, None, 0.5)
        self.assertEqual(a.status, "unknown")
        self.assertIsNone(a.pane_id)
        self.assertIn("no listening port", warn)

    def test_child_busy_makes_agent_busy(self):
        routes = {
            "/session": [{"id": "ses_1", "directory": "/w", "time": {"updated": NOW}}],
            "/session/status": {"ses_child": {"type": "busy"}},
            "/permission": [], "/question": [],
            "/session/ses_1/message": [],
        }
        srv = self.serve(routes)
        port = srv.server_address[1]
        a, _ = oc._agent(5, (), {}, "/w", port, 2.0)
        self.assertEqual(a.status, "busy")

    def two_session_routes(self, status=None):
        msg = lambda text, tok: [{"info": {"role": "assistant", "time": {"created": NOW}, "tokens": {"total": tok}},
                                  "parts": [{"type": "text", "text": text}]}]
        return {
            "/session": [{"id": "ses_1", "directory": "/w", "title": "First task", "time": {"updated": NOW}},
                         {"id": "ses_2", "directory": "/w", "title": "Second task with a long title",
                          "time": {"updated": NOW - 1000}}],
            "/session/status": status or {},
            "/permission": [], "/question": [],
            "/session/ses_1/message": msg("one", 111),
            "/session/ses_2/message": msg("two", 222),
        }

    def test_same_dir_agents_disambiguated_by_pane_title(self):
        srv = self.serve(self.two_session_routes())
        port = srv.server_address[1]
        env = {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "1"}
        a1, _ = oc._agent(1, (), env, "/w", port, 2.0, "OC | First task")
        a2, _ = oc._agent(2, (), env, "/w", port, 2.0, "OC | Second task with a lo...")
        self.assertEqual((a1.title, a1.session_id, a1.last_message, a1.context_tokens),
                         ("First task", "ses_1", "one", 111))
        self.assertEqual((a2.title, a2.session_id, a2.last_message, a2.context_tokens),
                         ("Second task with a long title", "ses_2", "two", 222))

    def test_home_screen_agent(self):
        srv = self.serve(self.two_session_routes())
        port = srv.server_address[1]
        a, warn = oc._agent(1, (), {}, "/w", port, 2.0, "OpenCode")
        self.assertIsNone(warn)
        self.assertEqual((a.title, a.session_id, a.last_message, a.context_tokens), (None, None, None, None))
        self.assertEqual((a.status, a.status_since_s), ("idle", None))
        self.assertTrue(a.meta["api_ok"])
        self.assertFalse(any(p.startswith("/session/ses_") for p, _ in srv.seen))

    def test_no_pane_title_falls_back_to_busy_then_recent(self):
        srv = self.serve(self.two_session_routes({"ses_2": {"type": "busy"}}))
        port = srv.server_address[1]
        a, _ = oc._agent(1, (), {}, "/w", port, 2.0)
        self.assertEqual((a.session_id, a.status), ("ses_2", "busy"))


class AttachParseTest(unittest.TestCase):
    OC = "/nix/store/x-opencode-1.18.27/bin/opencode"

    def test_variants(self):
        P = oc.parse_attach
        live = (self.OC, "attach", "http://127.0.0.1:5001/", "--session", "ses_edebc0000ffeM4ZHGtRd9HuNzQ",
                "--dir", "/Users/me/Projects/web-app")
        self.assertEqual(P(live), oc.AttachTarget("http://127.0.0.1:5001/", 5001, "ses_edebc0000ffeM4ZHGtRd9HuNzQ",
                                                  "/Users/me/Projects/web-app"))
        cases = [
            (("opencode", "attach", "http://localhost:4096", "-s", "ses_a"), (4096, "ses_a", None, None)),
            (("opencode", "attach", "--session=ses_b", "--dir=rel/dir", "http://127.0.0.1:5000"), (5000, "ses_b", "rel/dir", None)),
            (("opencode", "--log-level", "DEBUG", "attach", "http://[::1]:5002/"), (5002, None, None, None)),
            (("opencode", "attach", "http://127.0.0.1:5003", "-c"), (5003, None, None, None)),
            (("opencode", "attach", "http://127.0.0.1:5004", "-s", "ses_c", "--fork"), (5004, None, None, None)),
            (("opencode", "attach", "http://LOCALHOST"), (80, None, None, None)),
            (("opencode", "attach", "--mini", "-p", "pw", "http://127.0.0.1:5005"), (5005, None, None, None)),
        ]
        for argv, (port, sid, d, err) in cases:
            with self.subTest(argv=argv):
                t = P(argv)
                self.assertEqual((t.port, t.session_id, t.directory, t.error), (port, sid, d, err))
        for argv, msg in [
            (("opencode", "attach", "http://build.example:4096", "-s", "ses_r"), "non-local server build.example"),
            (("opencode", "attach", "http://10.0.0.5:4096"), "non-local server 10.0.0.5"),
            (("opencode", "attach", "https://localhost:4096"), "over https"),
            (("opencode", "attach", "http://127.0.0.1:99999"), "bad attach url"),
            (("opencode", "attach"), "bad attach url"),
            (("opencode", "attach", "localhost:4096"), "bad attach url"),
        ]:
            with self.subTest(argv=argv):
                t = P(argv)
                self.assertIsNone(t.port)
                self.assertIn(msg, t.error)
        for argv in [("opencode", "--port", "5000", "--auto"), ("opencode",), ("opencode", "run", "attach"),
                     ("opencode", "--dir", "attach")]:
            self.assertIsNone(P(argv), argv)
        # opencode attach is a server-less client: no listening port of its own, ever
        self.assertIsNone(oc.port_from_argv(("opencode", "attach", "http://127.0.0.1:5001/")))


class AttachAgentTest(unittest.TestCase):
    """An attach client on a server shared with the TUI's own sessions: status/title from its
    --session only."""
    SID = "ses_child"
    serve = AgentHttpTest.serve

    def routes(self, statuses=None, permission=(), children=()):
        now = int(time.time() * 1000)
        msg = [{"info": {"role": "assistant", "time": {"created": now - 65_000}, "tokens": {"total": 7}},
                "parts": [{"type": "text", "text": "child says"}]}]
        root = {"id": "ses_root", "directory": "/w", "title": "Root task", "time": {"updated": now}}
        return {
            # the TUI's busy root session; ours is a subagent session (has parentID)
            "/session": [root],
            "/session/ses_root": root,
            f"/session/{self.SID}": {"id": self.SID, "parentID": "ses_root", "directory": "/w",
                                     "title": "Inspect runner (@explorer subagent)", "time": {"updated": now - 65_000}},
            "/session/status": statuses if statuses is not None else {"ses_root": {"type": "busy"}},
            "/permission": list(permission), "/question": [],
            f"/session/{self.SID}/children": list(children),
            f"/session/{self.SID}/message": msg,
            "/session/ses_root/message": [],
        }

    def agent(self, routes, pane_title=None, sid=SID):
        srv = self.serve(routes)
        port = srv.server_address[1]
        argv = ("opencode", "attach", f"http://127.0.0.1:{port}/") + (("--session", sid) if sid else ()) + ("--dir", "/w")
        a, warn = oc._agent(9, argv, {}, "/w", port, 2.0, pane_title, attach=oc.parse_attach(argv))
        return a, warn, srv

    def test_status_and_title_by_session_id(self):
        a, warn, srv = self.agent(self.routes())
        self.assertIsNone(warn)
        self.assertTrue(a.meta["api_ok"])
        # the server's other (root) session is busy: not ours
        self.assertEqual((a.status, a.title, a.session_id), ("idle", "Inspect runner (@explorer subagent)", self.SID))
        self.assertEqual((a.last_message, a.context_tokens), ("child says", 7))
        self.assertEqual(a.status_since_s // 10, 6)
        paths = [p for p, _ in srv.seen]
        self.assertIn(f"/session/{self.SID}", paths)
        self.assertNotIn("/session", paths)                     # no "latest session in cwd" guess
        for _p, q in srv.seen:
            self.assertEqual(q.get("directory"), ["/w"])

    def test_own_busy_and_subagent_busy_and_pending(self):
        a, _w, _s = self.agent(self.routes({self.SID: {"type": "busy"}}))
        self.assertEqual(a.status, "busy")
        a, _w, _s = self.agent(self.routes({"ses_grandkid": {"type": "busy"}}, children=[{"id": "ses_grandkid"}]))
        self.assertEqual(a.status, "busy")
        a, _w, _s = self.agent(self.routes(permission=[{"id": "per_1", "sessionID": "ses_root"}]))
        self.assertEqual(a.status, "idle")                     # another session's request
        a, _w, _s = self.agent(self.routes(permission=[{"id": "per_2", "sessionID": self.SID}]))
        self.assertEqual(a.status, "needs_input")

    def test_session_gone(self):
        r = self.routes()
        del r[f"/session/{self.SID}"]
        a, warn, _s = self.agent(r)
        self.assertTrue(a.meta["api_ok"])
        self.assertEqual((a.status, a.title, a.session_id), ("idle", None, None))
        self.assertIn("not found", warn)

    def test_pane_title_shows_another_session(self):
        # the user switched sessions inside the attach TUI: its pane title wins over --session
        a, _w, _s = self.agent(self.routes(), pane_title="OC | Root task")
        self.assertEqual((a.session_id, a.title, a.status), ("ses_root", "Root task", "busy"))
        a, _w, _s = self.agent(self.routes(), pane_title="OC | Inspect runner (@explo...")
        self.assertEqual(a.session_id, self.SID)

    def test_continue_without_session_id_uses_pane_title(self):
        a, _w, _s = self.agent(self.routes(), pane_title="OC | Root task", sid=None)
        self.assertEqual((a.session_id, a.status), ("ses_root", "busy"))

    def test_non_local_attach_has_no_status(self):
        argv = ("opencode", "attach", "http://build.example:4096", "-s", "ses_x")
        a, warn = oc._agent(9, argv, {}, "/w", None, 1.0, attach=oc.parse_attach(argv))
        self.assertEqual((a.meta["port"], a.status, a.meta["api_ok"]), (None, "unknown", False))
        self.assertIn("non-local server build.example", warn)
        self.assertNotIn("no listening port", warn)


class FetchAttachTest(unittest.TestCase):
    def test_attach_port_and_dir_without_lsof(self):
        argvs = {20: ("opencode", "--port", "5001", "--auto"),
                 21: ("opencode", "attach", "http://127.0.0.1:5001/", "--session", "ses_a", "--dir", "/p/repo"),
                 22: ("opencode", "attach", "http://127.0.0.1:5001/", "--dir", "sub"),
                 23: ("opencode", "attach", "http://example.com:5001/")}
        seen = {}

        def fake_agent(pid, argv, env, cwd, port, timeout, pane_title=None, start=None, attach=None):
            seen[pid] = (cwd, port, attach.session_id if attach else None)
            return oc.make_agent(pid=pid, cwd=cwd, port=port, session_name=None, pane_id=None, start_time=start), None
        with mock.patch.object(oc.procmod, "list_procs", return_value=[(p, 1, " ".join(a)) for p, a in argvs.items()]), \
                mock.patch.object(oc, "find_opencode", return_value=list(argvs)), \
                mock.patch.object(oc.procmod, "proc_args_env", side_effect=lambda p: (argvs[p], {})), \
                mock.patch.object(oc.procmod, "proc_cwd", return_value={20: "/p/repo", 21: "/home", 22: "/p/repo"}), \
                mock.patch.object(oc.procmod, "listening_ports", side_effect=AssertionError("lsof")), \
                mock.patch.object(oc, "_agent", side_effect=fake_agent):
            agents, _ = oc.fetch()
        self.assertEqual(seen, {20: ("/p/repo", 5001, None), 21: ("/p/repo", 5001, "ses_a"),
                                22: ("/p/repo/sub", 5001, None), 23: (None, None, None)})


class _RawHandler(BaseHTTPRequestHandler):
    """Programmable responses: the class attribute `script(handler)` writes the raw response."""
    protocol_version = "HTTP/1.1"
    script = None

    def do_GET(self):  # noqa: N802
        self.server.hits.append(self.path)
        type(self).script(self)

    def log_message(self, *a):
        pass


class HttpClientTest(unittest.TestCase):
    def serve(self, script):
        handler = type("H", (_RawHandler,), {"script": staticmethod(script)})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        srv.daemon_threads = True
        srv.hits = []
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    @staticmethod
    def json_body(h, obj, chunked=False):
        data = json.dumps(obj).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        if chunked:
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            for i in range(0, len(data), 7):
                part = data[i:i + 7]
                h.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            h.wfile.write(b"0\r\n\r\n")
        else:
            h.send_header("Content-Length", str(len(data)))
            h.end_headers()
            h.wfile.write(data)

    def test_redirect_is_never_followed(self):
        other = self.serve(lambda h: self.json_body(h, []))   # stands in for the "external" target
        target = f"http://localhost:{other.server_address[1]}/session"

        def redirect(h):
            h.send_response(302)
            h.send_header("Location", target)
            h.send_header("Content-Length", "0")
            h.end_headers()

        srv = self.serve(redirect)
        port = srv.server_address[1]
        real = oc.socket.create_connection
        dialed = []

        def spy(addr, *a, **kw):
            dialed.append(addr)
            return real(addr, *a, **kw)

        for status_target in (target, "http://example.com/session"):
            with mock.patch.object(oc.socket, "create_connection", spy):
                with self.assertRaises(oc._ApiDown) as cm:
                    oc._get(port, "/session", None, 1.0)
            self.assertIn("HTTP 302", str(cm.exception))
        self.assertEqual(dialed, [("127.0.0.1", port)] * 2)
        self.assertEqual(other.hits, [])
        self.assertEqual(len(srv.hits), 2)
        # in _agent a redirect means "API unreachable"
        a, warn = oc._agent(1, (), {}, "/w", port, 1.0)
        self.assertFalse(a.meta["api_ok"])
        self.assertIn("HTTP 302", warn)

    def test_content_length_and_chunked(self):
        srv = self.serve(lambda h: self.json_body(h, {"x": list(range(50))}))
        self.assertEqual(oc._get(srv.server_address[1], "/a", {"q": "1"}, 1.0), {"x": list(range(50))})
        self.assertEqual(srv.hits, ["/a?q=1"])
        srv2 = self.serve(lambda h: self.json_body(h, {"y": "z" * 100}, chunked=True))
        self.assertEqual(oc._get(srv2.server_address[1], "/b", None, 1.0), {"y": "z" * 100})

    def test_non_200(self):
        def nf(h):
            h.send_response(404)
            h.send_header("Content-Length", "0")
            h.end_headers()
        srv = self.serve(nf)
        with self.assertRaises(oc._ApiDown):
            oc._get(srv.server_address[1], "/x", None, 1.0)

    def test_size_cap(self):
        srv = self.serve(lambda h: self.json_body(h, ["a" * 1000] * 200))
        with self.assertRaises(oc._ApiDown) as cm:
            oc.http_get(srv.server_address[1], "/big", 2.0, max_bytes=10_000)
        self.assertIn("larger than", str(cm.exception))

    def test_whole_response_deadline(self):
        import time

        def trickle(h):
            h.send_response(200)
            h.send_header("Content-Length", "1000")
            h.end_headers()
            try:
                for _ in range(100):
                    h.wfile.write(b" ")
                    h.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

        srv = self.serve(trickle)
        t0 = time.monotonic()
        with self.assertRaises(oc._ApiDown) as cm:
            oc.http_get(srv.server_address[1], "/slow", 0.4)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.7)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertIn("deadline", str(cm.exception))


class HttpOpenTest(unittest.TestCase):
    """oc.http_open: streaming GET (headers within the deadline, body left to the caller)."""
    serve = HttpClientTest.serve

    def test_returns_socket_headers_and_initial_body(self):
        def stream(h):
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            h.wfile.write(b"5\r\ndata:\r\n")
            h.wfile.flush()
            time.sleep(0.3)

        srv = self.serve(stream)
        sock, headers, body = oc.http_open(srv.server_address[1], "/event?directory=%2Fw", 1.0)
        try:
            self.assertEqual(headers["content-type"], "text/event-stream")
            if not body:
                sock.settimeout(1.0)
                body = sock.recv(100)
            self.assertTrue(body.startswith(b"5\r\n"))
        finally:
            sock.close()
        self.assertEqual(srv.hits, ["/event?directory=%2Fw"])

    def test_non_200_and_redirect_raise(self):
        def redirect(h):
            h.send_response(302)
            h.send_header("Location", "http://example.com/event")
            h.send_header("Content-Length", "0")
            h.end_headers()

        srv = self.serve(redirect)
        with self.assertRaises(oc.ApiDown) as cm:
            oc.http_open(srv.server_address[1], "/event", 1.0)
        self.assertIn("302", str(cm.exception))

    def test_header_deadline(self):
        srv = self.serve(lambda h: time.sleep(1.0))
        t0 = time.monotonic()
        with self.assertRaises(oc.ApiDown):
            oc.http_open(srv.server_address[1], "/event", 0.2)
        self.assertLess(time.monotonic() - t0, 0.6)


class FetchPaneTitlesTest(unittest.TestCase):
    def test_titles_passed_per_pane(self):
        envs = {10: {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "2"},
                11: {"ZELLIJ_SESSION_NAME": "t", "ZELLIJ_PANE_ID": "2"},
                12: {}}
        seen = {}

        def fake_agent(pid, argv, env, cwd, port, timeout, pane_title=None, start=None, attach=None):
            seen[pid] = pane_title
            return oc.make_agent(pid=pid, cwd=cwd, port=port, session_name=None, pane_id=None, start_time=start), None

        titles = {("s", 2): "OC | A", ("t", 2): "OpenCode", ("s", 3): "x"}
        with mock.patch.object(oc.procmod, "list_procs", return_value=[(p, 1, "opencode") for p in envs]), \
                mock.patch.object(oc, "find_opencode", return_value=list(envs)), \
                mock.patch.object(oc.procmod, "proc_args_env", side_effect=lambda p: (("opencode", "--port", "1"), envs[p])), \
                mock.patch.object(oc, "is_opencode_argv", return_value=True), \
                mock.patch.object(oc.procmod, "proc_cwd", return_value={}), \
                mock.patch.object(oc, "_agent", side_effect=fake_agent):
            agents, _ = oc.fetch(pane_titles=lambda: titles)
            self.assertEqual(seen, {10: "OC | A", 11: "OpenCode", 12: None})
            seen.clear()
            oc.fetch(pane_titles=titles)
            self.assertEqual(seen, {10: "OC | A", 11: "OpenCode", 12: None})
            seen.clear()
            oc.fetch()
            self.assertEqual(seen, {10: None, 11: None, 12: None})
        self.assertEqual([a.pid for a in agents], [10, 11, 12])


class FetchStartTimeTest(unittest.TestCase):
    """Agent.start_time = process start time (pid-reuse identity), also when the API call fails."""

    def patches(self, agent_fn):
        envs = {10: {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "2"}, 11: {}, 12: {}}
        return [
            mock.patch.object(oc.procmod, "list_procs", return_value=[(p, 1, "opencode") for p in envs]),
            mock.patch.object(oc, "find_opencode", return_value=list(envs)),
            mock.patch.object(oc.procmod, "proc_args_env",
                              side_effect=lambda p: (("opencode", "--port", str(5000 + p)), envs[p])),
            mock.patch.object(oc, "is_opencode_argv", return_value=True),
            mock.patch.object(oc.procmod, "proc_cwd", return_value={10: "/w"}),
            mock.patch.object(oc.procmod, "start_times", return_value={10: 1700000000.25, 11: 1700000001.5}),
            mock.patch.object(oc, "_agent", side_effect=agent_fn),
        ]

    def fetch(self, agent_fn):
        import contextlib
        with contextlib.ExitStack() as st:
            for p in self.patches(agent_fn):
                st.enter_context(p)
            return oc.fetch()

    def test_start_time_passed_and_set(self):
        real = oc._agent

        def agent_fn(pid, argv, env, cwd, port, timeout, pane_title=None, start=None, attach=None):
            # port 1 -> API unreachable fast; exercises the real record construction
            return real(pid, argv, env, cwd, None, timeout, pane_title, start)
        agents, _ = self.fetch(agent_fn)
        self.assertEqual({a.pid: a.start_time for a in agents}, {10: 1700000000.25, 11: 1700000001.5, 12: None})

    def test_start_time_on_failed_agent(self):
        def boom(*a, **k):
            raise RuntimeError("x")
        agents, warns = self.fetch(boom)
        self.assertEqual({a.pid: a.start_time for a in agents}, {10: 1700000000.25, 11: 1700000001.5, 12: None})
        self.assertEqual(len(warns), 3)

    def test_real_start_time_of_live_process(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        t = oc.procmod.start_time(child.pid)
        self.assertIsNotNone(t)
        self.assertLess(abs(t - time.time()), 5)


class FetchNoProcsTest(unittest.TestCase):
    def test_no_opencode(self):
        with mock.patch.object(oc.procmod, "list_procs", return_value=[(1, 0, "/sbin/launchd")]):
            self.assertEqual(oc.fetch(), ((), []))


if __name__ == "__main__":
    unittest.main()
