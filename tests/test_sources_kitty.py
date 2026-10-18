import json
import os
import unittest

from agentree.sources import kitty as k

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "live")


def fx(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def ls_json(windows, ow_focused=True, tab_focused=True):
    return json.dumps([{"id": 1, "is_focused": ow_focused, "tabs": [
        {"id": 10, "is_focused": tab_focused, "windows": windows}]}])


def win(wid, title, cmdline, overridden=False, focused=True):
    return {"id": wid, "title": title, "title_overridden": overridden, "is_focused": focused,
            "foreground_processes": [{"pid": 1, "cmdline": c} for c in cmdline]}


SOCK = "unix:/tmp/kitty.sock-1"


class SessionFromArgvTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            ("zellij", "attach", "s1"): (True, "s1"),
            ("/opt/homebrew/bin/zellij", "attach", "s1"): (True, "s1"),
            ("zellij", "a", "s1"): (True, "s1"),
            ("zellij", "attach", "--create", "s1"): (True, "s1"),
            ("zellij", "attach", "-c", "s1"): (True, "s1"),
            ("zellij", "attach", "-c", "s1", "options", "--theme", "x"): (True, "s1"),
            ("zellij", "-s", "s2"): (True, "s2"),
            ("zellij", "--session", "s2"): (True, "s2"),
            ("zellij", "--session=s2"): (True, "s2"),
            ("zellij", "-l", "agent", "-s", "s3"): (True, "s3"),
            ("zellij", "--config", "/x.kdl", "attach", "s4"): (True, "s4"),
            ("zellij",): (True, None),
            ("zellij", "attach"): (True, None),
            ("zellij", "action", "list-tabs"): (False, None),
            ("zellij", "list-sessions"): (False, None),
            ("zellij", "--server", "/tmp/x"): (False, None),
            ("fish",): (False, None),
            (): (False, None),
        }
        for argv, want in cases.items():
            self.assertEqual(k.session_from_argv(argv), want, argv)


class ParseLsTest(unittest.TestCase):
    def test_live(self):
        sessions = {"web-app", "dev"}
        sock = fx("kitty_socket.txt").strip()
        hosts = k.parse_ls(fx("kitty_ls.json"), sock, sessions)
        self.assertEqual({h.session for h in hosts}, sessions)
        for h in hosts:
            self.assertEqual(h.socket, sock)
            self.assertGreater(h.window_id, 0)
        data = json.loads(fx("kitty_ls.json"))
        # the opencode-only window (no zellij) must not be a host
        win_ids = {h.window_id for h in hosts}
        for ow in data:
            for t in ow["tabs"]:
                for w in t["windows"]:
                    if not any(os.path.basename((p.get("cmdline") or [""])[0]) == "zellij"
                               for p in w["foreground_processes"]):
                        self.assertNotIn(w["id"], win_ids)
        # at most one focused host
        self.assertLessEqual(sum(h.focused for h in hosts), 1)

    def test_argv_and_title_agree(self):
        h = k.parse_ls(ls_json([win(5, "s1 | nvim", [["zellij", "attach", "s1"]])]), SOCK, {"s1"})
        self.assertEqual([(x.session, x.window_id, x.tab_id, x.os_window_id, x.focused) for x in h],
                         [("s1", 5, 10, 1, True)])

    def test_argv_needs_corroborating_title(self):
        # argv alone may be stale: a title that names no session (or no title) is not enough
        h = k.parse_ls(ls_json([win(5, "something", [["zellij", "attach", "s1"]])]), SOCK, {"s1"})
        self.assertEqual(h, [])
        h = k.parse_ls(ls_json([win(5, "", [["zellij", "attach", "s1"]])]), SOCK, {"s1"})
        self.assertEqual(h, [])

    def test_argv_only_rejected_when_title_overridden(self):
        h = k.parse_ls(ls_json([win(5, "my title", [["zellij", "attach", "s1"]], overridden=True)]), SOCK, {"s1"})
        self.assertEqual(h, [])

    def test_overridden_title_is_ignored_even_if_it_agrees(self):
        # title_overridden => the title is the user's, cannot corroborate; argv may be stale
        h = k.parse_ls(ls_json([win(5, "s1 | x", [["zellij", "attach", "s1"]], overridden=True)]), SOCK, {"s1"})
        self.assertEqual(h, [])

    def test_title_names_other_known_session(self):
        # stale argv: client attached to s1 then switched to s2 inside zellij
        h = k.parse_ls(ls_json([win(5, "s2 | fish", [["zellij", "attach", "s1"]])]), SOCK, {"s1", "s2"})
        self.assertEqual(h, [])

    def test_contradiction_rejected_regardless_of_inventory(self):
        # title prefix contradicts argv: reject even if the title's session is unknown
        h = k.parse_ls(ls_json([win(5, "weird | fish", [["zellij", "attach", "s1"]])]), SOCK, {"s1"})
        self.assertEqual(h, [])
        h = k.parse_ls(ls_json([win(5, "s2 | fish", [["zellij", "attach", "s1"]])]), SOCK, set())
        self.assertEqual(h, [])

    def test_named_argv_accepted_when_title_agrees_even_if_not_in_inventory(self):
        h = k.parse_ls(ls_json([win(5, "s9 | fish", [["zellij", "-s", "s9"]])]), SOCK, set())
        self.assertEqual([x.session for x in h], ["s9"])

    def test_plain_zellij_title_only(self):
        h = k.parse_ls(ls_json([win(5, "s3 | fish", [["zellij"]])]), SOCK, {"s3"})
        self.assertEqual([x.session for x in h], ["s3"])
        self.assertEqual(k.parse_ls(ls_json([win(5, "s3 | fish", [["zellij"]], overridden=True)]), SOCK, {"s3"}), [])
        self.assertEqual(k.parse_ls(ls_json([win(5, "zz | fish", [["zellij"]])]), SOCK, {"s3"}), [])

    def test_title_only_without_zellij_process(self):
        self.assertEqual(k.parse_ls(ls_json([win(5, "s3 | fish", [["fish"]])]), SOCK, {"s3"}), [])

    def test_focus_requires_all_levels(self):
        w = [win(5, "s1 | x", [["zellij", "attach", "s1"]], focused=True)]
        self.assertFalse(k.parse_ls(ls_json(w, ow_focused=False), SOCK, {"s1"})[0].focused)
        self.assertFalse(k.parse_ls(ls_json(w, tab_focused=False), SOCK, {"s1"})[0].focused)
        w2 = [win(5, "s1 | x", [["zellij", "attach", "s1"]], focused=False)]
        self.assertFalse(k.parse_ls(ls_json(w2), SOCK, {"s1"})[0].focused)

    def test_plain_zellij_needs_client_and_known_session(self):
        # attach without a name: still a client, title must name a known session
        self.assertEqual([x.session for x in k.parse_ls(ls_json([win(5, "s3 | x", [["zellij", "attach"]])]),
                                                       SOCK, {"s3"})], ["s3"])
        # `zellij action ...` in the foreground is not a client
        self.assertEqual(k.parse_ls(ls_json([win(5, "s3 | x", [["zellij", "action", "list-tabs"]])]),
                                    SOCK, {"s3"}), [])

    def test_agentree_launched_window_with_default_title(self):
        def w(title, var, argv=("zellij", "attach", "s1"), overridden=False):
            d = win(5, title, [list(argv)], overridden=overridden)
            d["user_vars"] = {"agentree_session": var} if var else {}
            return d

        ok = k.parse_ls(ls_json([w("zellij", "s1")]), SOCK, set())
        self.assertEqual([x.session for x in ok], ["s1"])
        self.assertEqual(k.parse_ls(ls_json([w("zellij", None)]), SOCK, {"s1"}), [])      # no marker
        self.assertEqual(k.parse_ls(ls_json([w("zellij", "s2")]), SOCK, {"s1"}), [])      # marker != argv
        self.assertEqual(k.parse_ls(ls_json([w("s2 | fish", "s1")]), SOCK, {"s1"}), [])   # title contradicts
        self.assertEqual(k.parse_ls(ls_json([w("zellij", "s1", overridden=True)]), SOCK, {"s1"}), [])
        self.assertEqual(k.parse_ls(ls_json([w("zellij", "s1", argv=("zellij",))]), SOCK, {"s1"}), [])

    def test_title_session(self):
        self.assertEqual(k.title_session("a | b | c"), "a")
        self.assertIsNone(k.title_session("no sep"))
        self.assertIsNone(k.title_session(None))

    def test_empty(self):
        self.assertEqual(k.parse_ls("", SOCK, set()), [])
        self.assertEqual(k.parse_ls("[]", SOCK, set()), [])


class SocketsTest(unittest.TestCase):
    def test_owned_socket_policy(self):
        import socket
        import tempfile
        from unittest import mock

        pid = os.getpid()
        made = []

        def mk(path):
            s = socket.socket(socket.AF_UNIX)
            s.bind(path)
            made.append((s, path))

        good = f"/tmp/kitty.sock-{pid}"
        if os.path.exists(good):
            self.skipTest(f"{good} exists")
        try:
            mk(good)
            with open(f"/tmp/kitty.sock-{pid}x", "w"):
                pass                                   # not <pid>
            dead = f"/tmp/kitty.sock-{2 ** 22 + 12345}"
            if not os.path.exists(dead):
                mk(dead)                               # pid not alive
            with tempfile.TemporaryDirectory(dir="/tmp") as d:
                elsewhere = os.path.join(d, f"kitty.sock-{pid}")
                mk(elsewhere)                          # not directly under /tmp
                self.assertEqual(k.sockets(f"/tmp/kitty.sock-{pid}*"), [f"unix:{good}"])
                self.assertEqual(k.sockets(os.path.join(d, "kitty.sock-*")), [])
                self.assertEqual(k.sockets(f"/tmp/kitty.sock-{2 ** 22 + 12345}"), [])
                with mock.patch.object(k.os, "getuid", return_value=os.getuid() + 1):
                    self.assertEqual(k.sockets(good), [])       # not owned by us
        finally:
            for s, path in made:
                s.close()
                if os.path.exists(path):
                    os.unlink(path)
            if os.path.exists(f"/tmp/kitty.sock-{pid}x"):
                os.unlink(f"/tmp/kitty.sock-{pid}x")

    def test_is_owned_socket_canonical(self):
        import socket
        import tempfile

        pid = os.getpid()
        good = f"/tmp/kitty.sock-{pid}"
        if os.path.exists(good):
            self.skipTest(f"{good} exists")
        sk = socket.socket(socket.AF_UNIX)
        sk.bind(good)
        try:
            for spec in (good, f"unix:{good}", f"unix:/private/tmp/kitty.sock-{pid}", f"unix:/tmp/../tmp/kitty.sock-{pid}"):
                self.assertTrue(k.is_owned_socket(spec), spec)
            self.assertTrue(k.same_socket(f"unix:/private/tmp/kitty.sock-{pid}", good))
            self.assertFalse(k.same_socket(good, "unix:/tmp/kitty.sock-1"))
            for spec in (None, "", f"tmp/kitty.sock-{pid}", f"unix:/var/tmp/kitty.sock-{pid}",
                         f"unix:/tmp/kitty.sock-{pid}x", "unix:/tmp/kitty.sock-1"):
                self.assertFalse(k.is_owned_socket(spec), spec)
            with tempfile.TemporaryDirectory(dir="/tmp") as d:
                link = os.path.join(d, f"kitty.sock-{pid}")
                os.symlink(good, link)                    # a symlink is not the socket itself
                self.assertFalse(k.is_owned_socket(link))
        finally:
            sk.close()
            os.unlink(good)

    def test_fetch_passes_glob(self):
        from unittest import mock
        with mock.patch.object(k, "sockets", return_value=[]) as sk:
            self.assertEqual(k.fetch({"s"}, 0.5, socket_glob="/tmp/x-*"), ((), []))
        sk.assert_called_once_with("/tmp/x-*")


if __name__ == "__main__":
    unittest.main()
