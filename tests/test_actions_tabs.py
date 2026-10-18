"""Open-tab action / ensure_tab: lock + fresh re-validation, exact command sequences (fake zellij/kitty)."""
import fcntl
import os
import tempfile
import unittest
from unittest import mock

try:
    from tests.test_actions_jump import (
        DIR, KT, ROOT, SOCK, WT, ZJ, R, make_world_and_snap, nav_for, row,
    )
except ImportError:  # discovery with -s tests and no top-level dir
    from test_actions_jump import DIR, KT, ROOT, SOCK, WT, ZJ, R, make_world_and_snap, nav_for, row  # type: ignore

from agentree.actions import tabs
from agentree.actions.runner import identity_key
from agentree.types import Orphans, Snapshot, WorktreeNode, ZSession

_orig_session_lock = tabs.session_lock


def _short_lock(path):
    return _orig_session_lock(path, 0.1)


class OpenTabTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        p = mock.patch.object(tabs.os.path, "isdir", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def open(self, w, snap, r, **kw):
        layout = kw.pop("layout", None)
        return tabs.open_tab(nav_for(w, **kw), r, snap, state_dir=self.state, layout=layout)

    def world_without_wt_panes(self, **kw):
        w, snap, node = make_world_and_snap(wt_tab=False, **kw)
        s = w.s("S")
        s["tabs"] = [(0, "main")]
        s["panes"] = [(0, 0, "/r", False), (9, 0, None, True)]
        return w, snap, node

    def test_existing_tab_jumps_instead(self):
        w, snap, node = make_world_and_snap(wt_tab=False)
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1")])
        self.assertIn("already open in tab x", res.message)
        self.assertTrue(os.path.exists(tabs.lock_path(self.state, DIR, "S")))

    def test_nested_worktree_pane_does_not_count_for_root(self):
        w, snap, node = make_world_and_snap()
        s = w.s("S")
        s["tabs"] = [(1, "x")]
        s["panes"] = [(1, 1, WT.path, False)]
        res = self.open(w, snap, row("worktree", WorktreeNode(ROOT), "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "new-tab", "--cwd", "/r", "--name", "r"),
                                    ("zj", "S", "go-to-tab-by-id", "2")])

    def test_creates_tab_in_current_session(self):
        w, snap, node = self.world_without_wt_panes()
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S", layout="agent")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "new-tab", "--cwd", WT.path, "--name", "x", "--layout", "agent"),
                                    ("zj", "S", "go-to-tab-by-id", "1")])
        new = w.index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "new-tab")
        self.assertEqual(w.calls[new][1], {"ZELLIJ_SOCKET_DIR": DIR})
        # clients re-queried immediately before new-tab
        self.assertEqual(w.calls[new - 1][0][4], "list-clients")

    def test_creates_tab_in_other_session_then_navigates(self):
        w, snap, node = self.world_without_wt_panes()
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "new-tab", "--cwd", WT.path, "--name", "x"),
                                    ("zj", "S", "go-to-tab-by-id", "1"),
                                    ("kitty", SOCK, "focus-window", "--match", "id:5")])

    def test_orphan_row_uses_current_session(self):
        w, snap, node = self.world_without_wt_panes()
        snap = Snapshot(snap.taken_at, snap.sessions, Orphans(worktrees=(WT,)))
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), None, key=f"ow:{WT.path}"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts()[0][:3], ("zj", "S", "new-tab"))
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), None), current=None)
        self.assertFalse(res.ok)
        self.assertIn("not running inside zellij", res.message)

    def test_current_session_identity_checked(self):
        w, snap, node = self.world_without_wt_panes()
        snap = Snapshot(snap.taken_at, (), Orphans(worktrees=(WT,)))      # not even in the snapshot
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), None), current="S", current_dir="/tmp/other")
        self.assertFalse(res.ok)
        self.assertIn("not live in /tmp/other", res.message)
        self.assertEqual(w.acts(), [])
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), None), current="S")
        self.assertTrue(res.ok, res.message)

    def test_detached_session_attaches_before_new_tab(self):
        w, snap, node = self.world_without_wt_panes(clients=0, host=False)
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual([a[2] for a in w.acts()], ["launch", "new-tab", "go-to-tab-by-id", "focus-window"])
        self.assertEqual(w.acts()[-1], ("kitty", SOCK, "focus-window", "--match", "id:77"))

    def test_duplicate_recheck_after_attachment(self):
        w, snap, node = self.world_without_wt_panes(clients=0, host=False)

        def someone_opened_it(world, name):      # e.g. resurrected layout / another tool
            world.s("S")["panes"].append((5, 0, WT.path, False))

        w.on_attach = someone_opened_it
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="OTHER")
        self.assertTrue(res.ok, res.message)
        self.assertNotIn("new-tab", [a[2] for a in w.acts()])
        self.assertIn("already open", res.message)

    def test_multi_client_no_focus_and_no_switch(self):
        w, snap, node = self.world_without_wt_panes(clients=2)
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="OTHER")
        self.assertTrue(res.ok, res.message)
        self.assertFalse(res.close)
        self.assertEqual(w.acts(), [("zj", "S", "new-tab", "--cwd", WT.path, "--name", "x", "--no-focus")])
        self.assertIn("2 clients", res.message)

    def test_never_new_tab_without_client(self):
        w, snap, node = self.world_without_wt_panes(clients=0, host=False)
        w.attach_clients = 0
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="OTHER")
        self.assertFalse(res.ok)
        self.assertNotIn("new-tab", [a[2] for a in w.acts()])
        w2, snap2, _ = self.world_without_wt_panes()
        w2.clients_fail = True
        res = self.open(w2, snap2, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertFalse(res.ok)
        self.assertEqual(w2.acts(), [])

    def test_second_open_does_not_duplicate(self):
        w, snap, node = self.world_without_wt_panes()
        self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        w.calls.clear()
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertTrue(res.ok)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1")])
        self.assertEqual([t for t in w.s("S")["tabs"] if t[1] == "x"], [(1, "x")])

    def test_refusals(self):
        w, snap, node = self.world_without_wt_panes(complete=False)
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("inventory incomplete", res.message)
        w, snap, node = self.world_without_wt_panes()
        w.s("S")["panes"].append((7, 42, "/x", False))
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertFalse(res.ok)
        self.assertEqual(w.acts(), [])
        res = self.open(w, snap, row("agent", None, "S"), current="S")
        self.assertIn("select a worktree", res.message)

    def test_new_tab_verification(self):
        w, snap, node = self.world_without_wt_panes()
        w.new_tab_out = "5\n"                     # printed an id it did not create
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertIn("does not list it", res.message)
        w.new_tab_out = "0\n"                     # an id that already existed
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertIn("existing tab 0", res.message)
        w.new_tab_out = None
        w.new_tab_name = "something-else"          # created, but not what we asked for
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("does not look like x", res.message)

    def test_new_tab_pane_listed_late(self):
        w, snap, node = self.world_without_wt_panes()
        orig = w.zellij
        state = {"hide": 0}

        def late(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "new-tab":
                state["hide"] = 2                   # next two pane listings miss the new pane
            elif argv[1] == "--session" and argv[4] == "list-panes" and state["hide"]:
                state["hide"] -= 1
                import json as _j
                panes = [p for p in _j.loads(r.stdout) if p["id"] < 100]
                return r.__class__(r.argv, r.code, _j.dumps(panes), r.stderr, r.timed_out, r.duration_s)
            return r

        w.zellij = late
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertTrue(res.ok, res.message)

    def test_lock_contention(self):
        w, snap, node = self.world_without_wt_panes()
        path = tabs.lock_path(self.state, DIR, "S")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh, mock.patch.object(tabs, "session_lock", _short_lock):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("another agentree", res.message)
        self.assertEqual(w.acts(), [])

    def test_dry_run(self):
        w, snap, node = self.world_without_wt_panes()
        res = self.open(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S", dry_run=True)
        self.assertTrue(res.ok)
        self.assertEqual(res.message, "dry-run: 1 command logged")
        self.assertEqual(len(w.s("S")["tabs"]), 1)

    def test_lock_path_hashed_identity(self):
        p = tabs.lock_path("/s", "/tmp/zellij-502", "a/b c")
        self.assertEqual(p, f"/s/locks/tab-{identity_key('/private/tmp/zellij-502', 'a/b c')}.lock")


class EnsureTabTest(unittest.TestCase):
    def test_reusable_primitive(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tabs.os.path, "isdir", return_value=True):
            w, snap, node = make_world_and_snap(wt_tab=False)
            w.s("S")["tabs"] = [(0, "main")]
            w.s("S")["panes"] = [(0, 0, "/r", False)]
            nav = nav_for(w, current="S")
            s = ZSession("S", "attached", 60, DIR, 1)
            r1 = tabs.ensure_tab(nav, s, WT.path, "feat", state_dir=d, roots={"/r"})
            self.assertEqual((r1.ok, r1.created, r1.tab_id), (True, True, 1))
            r2 = tabs.ensure_tab(nav, s, WT.path, "feat", state_dir=d, roots={"/r"})
            self.assertEqual((r2.ok, r2.created, r2.tab_id), (True, False, 1))
            w.s("S")["clients"] = 0
            r3 = tabs.ensure_tab(nav, s, "/r/other", "o", state_dir=d, attach=False)
            self.assertFalse(r3.ok)
            self.assertIn("no client", r3.message)


if __name__ == "__main__":
    unittest.main()


class EnsureTabCreatedIdTest(unittest.TestCase):
    """Gate M3 attempt 2, finding 4: a printed tab id is never lost after new-tab."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def world(self):
        w, snap, node = make_world_and_snap(wt_tab=False)
        w.s("S")["tabs"] = [(0, "main")]
        w.s("S")["panes"] = [(0, 0, "/r", False)]
        return w

    def test_listing_failure_after_new_tab_returns_the_id(self):
        w = self.world()
        orig = w.zellij
        order = []

        def z(argv, env):
            if argv[1] == "--session" and argv[4] in ("list-tabs", "list-panes"):
                order.append(argv[4])
                if "new-tab" in order:
                    return R(argv, 1, err="connection reset")
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "new-tab":
                order.append("new-tab")
            return r
        w.zellij = z
        nav = nav_for(w, current="S")
        s = ZSession("S", "attached", 60, DIR, 1)
        res = tabs.ensure_tab(nav, s, WT.path, "x", state_dir=self.tmp.name, roots={"/r"},
                              on_created=lambda tid: order.append(f"journal:{tid}"))
        self.assertFalse(res.ok)
        self.assertEqual((res.tab_id, res.created), (1, True))
        self.assertIn("tab 1 created but not verified", res.message)
        i = order.index("new-tab")
        self.assertEqual(order[i + 1], "journal:1")          # before any further query

    def test_failed_new_tab_with_printed_id(self):
        w = self.world()
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "new-tab":
                return R(argv, 1, out=r.stdout, err="layout error")
            return r
        w.zellij = z
        got = []
        res = tabs.ensure_tab(nav_for(w, current="S"), ZSession("S", "attached", 60, DIR, 1), WT.path, "x",
                              state_dir=self.tmp.name, on_created=got.append)
        self.assertEqual((res.ok, res.tab_id, res.created, got), (False, 1, True, [1]))

    def test_no_id_no_callback(self):
        w = self.world()
        w.new_tab_out = "garbage"
        got = []
        res = tabs.ensure_tab(nav_for(w, current="S"), ZSession("S", "attached", 60, DIR, 1), WT.path, "x",
                              state_dir=self.tmp.name, on_created=got.append)
        self.assertEqual((res.ok, res.tab_id, got), (False, None, []))

    def test_verification_raising_any_exception_returns_the_id(self):
        w = self.world()
        nav = nav_for(w, current="S")
        orig = nav.fresh_inventory
        state = {"after": False}

        def inv(s):
            if state["after"]:
                raise ValueError("unparseable listing")
            return orig(s)
        nav.fresh_inventory = inv
        got = []

        def created(tid):
            got.append(tid)
            state["after"] = True
        res = tabs.ensure_tab(nav, ZSession("S", "attached", 60, DIR, 1), WT.path, "x",
                              state_dir=self.tmp.name, roots={"/r"}, on_created=created)
        self.assertEqual((res.ok, res.tab_id, res.created, res.verified, got), (False, 1, True, False, [1]))
        self.assertIn("tab 1 created but not verified: ValueError", res.message)

    def test_on_created_failure_still_returns_the_id(self):
        w = self.world()

        def boom(tid):
            raise OSError("journal gone")
        res = tabs.ensure_tab(nav_for(w, current="S"), ZSession("S", "attached", 60, DIR, 1), WT.path, "x",
                              state_dir=self.tmp.name, roots={"/r"}, on_created=boom)
        self.assertEqual((res.ok, res.tab_id, res.created, res.verified), (False, 1, True, False))

    def test_existing_id_is_not_reported_as_created(self):
        w = self.world()
        w.new_tab_out = "0\n"
        got = []
        res = tabs.ensure_tab(nav_for(w, current="S"), ZSession("S", "attached", 60, DIR, 1), WT.path, "x",
                              state_dir=self.tmp.name, on_created=got.append)
        self.assertEqual((res.ok, res.tab_id, res.created, got), (False, None, False, []))
        self.assertIn("existing tab 0", res.message)

    def test_verified_flag_on_success(self):
        w = self.world()
        res = tabs.ensure_tab(nav_for(w, current="S"), ZSession("S", "attached", 60, DIR, 1), WT.path, "x",
                              state_dir=self.tmp.name, roots={"/r"})
        self.assertEqual((res.ok, res.created, res.verified), (True, True, True))
