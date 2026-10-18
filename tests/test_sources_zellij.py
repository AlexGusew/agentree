import json
import os
import tempfile
import unittest
from unittest import mock

from agentree.sources import zellij as z
from agentree.types import ZPane

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")


def fx(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


class ParseListSessionsTest(unittest.TestCase):
    def test_live_tmp_dir(self):
        rows = z.parse_list_sessions(fx("zellij_list_sessions_tmp.txt"))
        names = [r.name for r in rows]
        self.assertIn("web-app", names)
        self.assertIn("jumping-cymbal", names)
        by = {r.name: r for r in rows}
        self.assertFalse(by["web-app"].exited)
        self.assertTrue(by["jumping-cymbal"].exited)
        self.assertGreater(by["jumping-cymbal"].created_age_s, 17 * 86400)
        for r in rows:
            self.assertIsNotNone(r.created_age_s)

    def test_live_tmpdir_dir(self):
        rows = {r.name: r for r in z.parse_list_sessions(fx("zellij_list_sessions_tmpdir.txt"))}
        self.assertFalse(rows["dev"].exited)
        self.assertTrue(rows["web-app"].exited)

    def test_synthetic_markers_and_units(self):
        text = (
            "a [Created 17days 6h 13m 47s ago] (EXITED - attach to resurrect)\n"
            "b [Created 1h 26m 6s ago] (current)\n"
            "c [Created 2weeks 1day 3s ago] \n"
            "d [Created 5s ago]\n"
            "\x1b[32;1me\x1b[m [Created \x1b[35;1m0s\x1b[m ago] \n"
            "\n"
        )
        rows = z.parse_list_sessions(text)
        self.assertEqual([r.name for r in rows], ["a", "b", "c", "d", "e"])
        a, b, c, d, e = rows
        self.assertEqual(a.created_age_s, 17 * 86400 + 6 * 3600 + 13 * 60 + 47)
        self.assertTrue(a.exited)
        self.assertFalse(a.is_current)
        self.assertEqual(b.created_age_s, 3600 + 26 * 60 + 6)
        self.assertTrue(b.is_current)
        self.assertFalse(b.exited)
        self.assertEqual(c.created_age_s, 2 * 604800 + 86400 + 3)
        self.assertEqual(d.created_age_s, 5)
        self.assertEqual(e.created_age_s, 0)

    def test_no_sessions(self):
        self.assertEqual(z.parse_list_sessions("No active zellij sessions found.\n"), [])
        self.assertEqual(z.parse_list_sessions(""), [])

    def test_name_only(self):
        rows = z.parse_list_sessions("plain\n")
        self.assertEqual(rows, [z.ParsedSession("plain", None, False, False)])


class ParseTabsPanesTest(unittest.TestCase):
    def test_live_tabs(self):
        tabs = z.parse_list_tabs(fx("zellij_web-app_list_tabs.json"))
        self.assertGreaterEqual(len(tabs), 1)
        self.assertEqual([t.position for t in tabs], sorted(t.position for t in tabs))
        self.assertEqual(sum(t.active for t in tabs), 1)
        self.assertEqual(tabs[0].panes, ())
        raw = json.loads(fx("zellij_web-app_list_tabs.json"))
        self.assertEqual({t.tab_id for t in tabs}, {r["tab_id"] for r in raw})

    def test_live_panes(self):
        panes = z.parse_list_panes(fx("zellij_web-app_list_panes.json"))
        plugins = [p for p in panes if p.is_plugin]
        terms = [p for p in panes if not p.is_plugin]
        self.assertTrue(plugins)
        self.assertTrue(terms)
        for p in plugins:
            self.assertIsNone(p.command)
            self.assertIsNone(p.cwd)
            self.assertTrue(p.zid.startswith("plugin_"))
        oc = [p for p in terms if p.command == "opencode"]
        self.assertTrue(oc)
        self.assertIn("--port", oc[0].argv)
        self.assertTrue(oc[0].cwd.startswith("/"))
        # default shell: terminal_command is null, pane_command gives "fish"
        raw = {r["id"]: r for r in json.loads(fx("zellij_web-app_list_panes.json"))
               if not r["is_plugin"]}
        for p in terms:
            if raw[p.pane_id]["terminal_command"] is None and raw[p.pane_id].get("pane_command"):
                self.assertEqual(p.command, os.path.basename(raw[p.pane_id]["pane_command"].split()[0]))

    def test_synthetic_pane(self):
        data = json.dumps([{
            "id": 7, "is_plugin": False, "title": "t", "terminal_command": "'/usr/bin/my nvim' -R 'a b'",
            "pane_cwd": "/tmp", "tab_id": 3, "is_focused": True, "is_floating": True, "exited": True,
            "is_selectable": False,
        }, {"id": 1, "is_plugin": False, "title": "x", "terminal_command": "unbalanced 'quote", "tab_id": 0}])
        p, q = z.parse_list_panes(data)
        self.assertEqual(p, ZPane(7, False, 3, "t", "my nvim", ("/usr/bin/my nvim", "-R", "a b"),
                                  os.path.realpath("/tmp"), True, True, True, False))
        self.assertEqual(p.zid, "terminal_7")
        self.assertEqual(q.argv, ("unbalanced", "'quote"))
        self.assertIsNone(q.cwd)

    def test_bad_json(self):
        with self.assertRaises(ValueError):
            z.parse_list_tabs("not json")
        with self.assertRaises(ValueError):
            z.parse_list_panes('{"a": 1}')


class ParseClientsTest(unittest.TestCase):
    def test_live(self):
        self.assertEqual(z.parse_list_clients(fx("zellij_web-app_list_clients.txt")), 1)

    def test_none_and_many(self):
        self.assertEqual(z.parse_list_clients("CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n"), 0)
        self.assertEqual(z.parse_list_clients(""), 0)
        self.assertEqual(z.parse_list_clients(
            "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1  terminal_2  fish\n3  plugin_1  \n"), 2)


class MergeTest(unittest.TestCase):
    def test_live_merge(self):
        per = {
            "/tmp/zellij-502": z.parse_list_sessions(fx("zellij_list_sessions_tmp.txt")),
            "/T/zellij-502": z.parse_list_sessions(fx("zellij_list_sessions_tmpdir.txt")),
        }
        merged = {s.name: (s, d) for s, d in z.merge_sessions(per)}
        s, d = merged["web-app"]
        self.assertFalse(s.exited)
        self.assertEqual(d, "/tmp/zellij-502")
        self.assertLess(s.created_age_s, 86400)  # age from the live listing, not the cache
        s, d = merged["dev"]
        self.assertFalse(s.exited)
        self.assertEqual(d, "/T/zellij-502")
        s, d = merged["jumping-cymbal"]
        self.assertTrue(s.exited)
        self.assertIsNone(d)
        self.assertEqual(len(merged), len({r.name for v in per.values() for r in v}))

    def test_order_independent(self):
        live = z.ParsedSession("x", 10, False, False)
        dead = z.ParsedSession("x", 999, True, False)
        self.assertEqual(z.merge_sessions({"A": [dead], "B": [live]}), [(live, "B")])
        self.assertEqual(z.merge_sessions({"B": [live], "A": [dead]}), [(live, "B")])

    def test_exited_everywhere(self):
        a = z.ParsedSession("x", 500, True, False)
        b = z.ParsedSession("x", 100, True, False)
        self.assertEqual(z.merge_sessions({"A": [a], "B": [b]}), [(b, None)])


    def test_live_duplicates_prefer_env_dir_else_first(self):
        a = z.ParsedSession("x", 10, False, False)
        b = z.ParsedSession("x", 20, False, False)
        dead = z.ParsedSession("y", 5, True, False)
        per = {"/A": [a, dead], "/B": [b, dead]}
        self.assertEqual(z.live_duplicates(per), {"x": ["/A", "/B"]})
        self.assertEqual(z.merge_sessions(per)[0], (a, "/A"))
        self.assertEqual(z.merge_sessions(per, "/B/")[0], (b, "/B"))
        self.assertEqual(z.live_duplicates({"/A": [a], "/B": [dead]}), {})


def R(stdout="", code=0, stderr=""):
    return z.ex.Result((), code, stdout, stderr, False, 0.0)


class FetchTest(unittest.TestCase):
    """fetch() with zellij calls faked from the live fixtures."""
    NAME = "web-app"

    def setUp(self):
        self.tabs = fx(f"zellij_{self.NAME}_list_tabs.json")
        self.panes = fx(f"zellij_{self.NAME}_list_panes.json")
        self.clients = fx(f"zellij_{self.NAME}_list_clients.txt")
        self.calls = []
        self.tab_outputs = None

    def fake_action(self, zj, d, name, args, timeout):
        self.calls.append((d, name, args[0]))
        if args[0] == "list-tabs":
            n = sum(1 for c in self.calls if c[1] == name and c[2] == "list-tabs")
            outs = self.tab_outputs or [self.tabs]
            return R(outs[min(n, len(outs)) - 1])
        if args[0] == "list-panes":
            return R(self.panes)
        return R(self.clients)

    def run_fetch(self, listings, env_dir=None, no_socket=(), current=""):
        def fake_ls(zj, d, timeout):
            return R(listings[d])

        env = {"ZELLIJ_SOCKET_DIR": env_dir} if env_dir else {}
        with mock.patch.object(z, "_list_sessions", fake_ls), \
                mock.patch.object(z, "has_socket", lambda d, n: (d, n) not in set(no_socket)), \
                mock.patch.object(z, "_session_action", self.fake_action), \
                mock.patch.object(z, "_zellij", return_value="zellij"), \
                mock.patch.dict(os.environ, env):
            if not env_dir:
                os.environ.pop("ZELLIJ_SOCKET_DIR", None)
            return z.fetch(list(listings), timeout=1.0, current_session=current)

    def without_tab(self, tab_id):
        return json.dumps([t for t in json.loads(self.tabs) if t["tab_id"] != tab_id])

    def test_consistent(self):
        sessions, warnings = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago]\n"})
        self.assertEqual(warnings, [])
        s = sessions[0]
        self.assertTrue(s.inventory_complete)
        self.assertEqual([len(t.panes) > 0 for t in s.tabs], [True, True, True])
        self.assertEqual(sum(1 for c in self.calls if c[2] == "list-tabs"), 1)

    def test_inconsistent_then_fixed_by_retry(self):
        self.tab_outputs = [self.without_tab(2), self.tabs]
        sessions, warnings = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago]\n"})
        self.assertEqual(warnings, [])
        self.assertTrue(sessions[0].inventory_complete)
        self.assertEqual(len(sessions[0].tabs), 3)
        self.assertEqual([c[2] for c in self.calls].count("list-tabs"), 2)
        self.assertEqual([c[2] for c in self.calls].count("list-panes"), 2)
        self.assertEqual([c[2] for c in self.calls].count("list-clients"), 1)

    def test_still_inconsistent_after_retry(self):
        self.tab_outputs = [self.without_tab(2)]
        sessions, warnings = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago]\n"})
        self.assertFalse(sessions[0].inventory_complete)
        self.assertEqual(len(sessions[0].tabs), 2)
        self.assertTrue(any("unknown tabs [2]" in w.message for w in warnings), warnings)
        self.assertEqual([c[2] for c in self.calls].count("list-tabs"), 2)

    def test_tabs_failure_retried(self):
        self.tab_outputs = ["not json", self.tabs]
        sessions, warnings = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago]\n"})
        self.assertTrue(sessions[0].inventory_complete)
        self.tab_outputs = ["not json"]
        self.calls.clear()
        sessions, warnings = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago]\n"})
        self.assertFalse(sessions[0].inventory_complete)
        self.assertTrue(any("list-tabs failed" in w.message for w in warnings))

    def test_live_in_two_dirs(self):
        listing = f"{self.NAME} [Created 1h ago]\nold [Created 9d ago] (EXITED - attach to resurrect)\n"
        sessions, warnings = self.run_fetch({"/A": listing, "/B": listing}, env_dir="/B")
        by = {s.name: s for s in sessions}
        s = by[self.NAME]
        self.assertEqual(s.socket_dir, "/B")
        self.assertFalse(s.inventory_complete)
        self.assertEqual(by["old"].state, "exited")
        dup = [w.message for w in warnings if "two socket dirs" in w.message]
        self.assertEqual(dup, [f"session {self.NAME} is live in two socket dirs (/A, /B); actions disabled"])
        self.assertEqual({c[0] for c in self.calls}, {"/B"})
        self.calls.clear()
        sessions, _ = self.run_fetch({"/A": listing, "/B": listing})
        self.assertEqual(sessions[0].socket_dir, "/A")     # no env: first configured dir


    def test_listed_live_without_socket_is_not_live_there(self):
        # zellij 0.45.1 run with the caller's ZELLIJ_SESSION_NAME lists that session "(current)" in
        # every socket dir; only the dir with its socket hosts it: no 2-dir warning, no actions lock
        listing = f"{self.NAME} [Created 1m ago] (current)\nold [Created 9d ago] (EXITED - attach to resurrect)\n"
        sessions, warnings = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago]\n", "/B": listing},
                                            env_dir="/B", no_socket={("/B", self.NAME)}, current=self.NAME)
        by = {s.name: s for s in sessions}
        s = by[self.NAME]
        self.assertEqual(s.socket_dir, "/A")
        self.assertTrue(s.inventory_complete)
        self.assertTrue(s.is_current)                      # from $ZELLIJ_SESSION_NAME, not "(current)"
        self.assertEqual(s.created_age_s, 3600)            # age from the dir that really hosts it
        self.assertEqual([w for w in warnings if "socket dirs" in w.message], [])
        self.assertEqual({c[0] for c in self.calls}, {"/A"})
        # without any socket the session is exited, never current
        self.calls.clear()
        sessions, warnings = self.run_fetch({"/B": listing}, no_socket={("/B", self.NAME)}, current=self.NAME)
        s = {x.name: x for x in sessions}[self.NAME]
        self.assertEqual((s.state, s.socket_dir, s.is_current), ("exited", None, False))
        self.assertEqual(self.calls, [])

    def test_current_marker_ignored(self):
        sessions, _ = self.run_fetch({"/A": f"{self.NAME} [Created 1h ago] (current)\n"})
        self.assertFalse(sessions[0].is_current)


class ListSessionsEnvTest(unittest.TestCase):
    def test_child_env_has_no_zellij_client_vars(self):
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "zellij")
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\nenv\n")
            os.chmod(fake, 0o755)
            env = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "me", "ZELLIJ_PANE_ID": "1", "AGENTREE_KEEP": "x"}
            with mock.patch.dict(os.environ, env):
                r = z._list_sessions(fake, "/sock/dir", 3.0)
            self.assertTrue(r.ok, r)
            got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            self.assertNotIn("ZELLIJ_SESSION_NAME", got)
            self.assertNotIn("ZELLIJ_PANE_ID", got)
            self.assertNotIn("ZELLIJ", got)
            self.assertEqual(got["ZELLIJ_SOCKET_DIR"], "/sock/dir")
            self.assertEqual(got["AGENTREE_KEEP"], "x")
        self.assertEqual(z.list_sessions_argv("zj")[-3:], ["zj", "list-sessions", "-n"])


class HasSocketTest(unittest.TestCase):
    def test_socket_file_required(self):
        import socket as so
        d = tempfile.mkdtemp(prefix="wz", dir="/tmp")          # short: AF_UNIX path limit
        self.addCleanup(__import__("shutil").rmtree, d, True)
        self.assertFalse(z.has_socket(d, "s"))                 # no contract dir
        cv = os.path.join(d, "contract_version_1")
        os.mkdir(cv)
        self.assertFalse(z.has_socket(d, "s"))                 # empty contract dir (the bug's $TMPDIR dir)
        open(os.path.join(cv, "f"), "w").close()
        self.assertFalse(z.has_socket(d, "f"))                 # a regular file is not a socket
        sk = so.socket(so.AF_UNIX)
        self.addCleanup(sk.close)
        sk.bind(os.path.join(cv, "s"))
        self.assertTrue(z.has_socket(d, "s"))
        self.assertFalse(z.has_socket(os.path.join(d, "missing"), "s"))
        per = {d: [z.ParsedSession("s", 1, False, False), z.ParsedSession("t", 2, False, True),
                   z.ParsedSession("u", 3, True, False)]}
        self.assertEqual(z.verify_live(per)[d], [z.ParsedSession("s", 1, False, False),
                                                 z.ParsedSession("t", 2, True, False),
                                                 z.ParsedSession("u", 3, True, False)])


class SocketDirsTest(unittest.TestCase):
    def test_configured(self):
        self.assertEqual(z.socket_dirs(["/tmp/x/", "/tmp//x", "/tmp/y"]), ["/tmp/x", "/tmp/y"])

    def test_defaults_dedupe_and_exist(self):
        with tempfile.TemporaryDirectory() as d:
            uid = os.getuid()
            os.mkdir(os.path.join(d, f"zellij-{uid}"))
            env = {"TMPDIR": d + "/", "ZELLIJ_SOCKET_DIR": os.path.join(d, f"zellij-{uid}") + "/"}
            with mock.patch.dict(os.environ, env):
                dirs = z.socket_dirs(None)
            want = os.path.join(d, f"zellij-{uid}")
            self.assertIn(want, dirs)
            self.assertEqual(len([x for x in dirs if os.path.realpath(x) == os.path.realpath(want)]), 1)
            for x in dirs:
                self.assertTrue(os.path.isdir(x))
                self.assertFalse(x.endswith("/"))


if __name__ == "__main__":
    unittest.main()
