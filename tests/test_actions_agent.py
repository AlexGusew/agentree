"""`o` new agent pane: fake zellij/kitty, exact command sequences, verification and refusals."""
import json
import os
import tempfile
import unittest
from unittest import mock

try:
    from tests.test_actions_jump import (
        DIR, KT, SOCK, WT, ZJ, R, World, fake_nav, make_world_and_snap, nav_for, row,
    )
    from tests.test_actions_spawn import FakeLinks
    from tests._fake_provider import CODEX, FakeProvider, inject
except ImportError:  # pragma: no cover
    from test_actions_jump import DIR, KT, SOCK, WT, ZJ, R, World, fake_nav, make_world_and_snap, nav_for, row  # type: ignore
    from test_actions_spawn import FakeLinks  # type: ignore
    from _fake_provider import CODEX, FakeProvider, inject  # type: ignore

from agentree.actions import agent as ag
from agentree.actions.api import AgentChoice, AgentOptions
from agentree.actions.real import RealActions
from agentree.config import Config
from agentree.providers import command, registry
from agentree.providers.base import ProviderSpec
from agentree.types import Agent, Orphans, Snapshot, WorktreeNode, ZPane

OC = "/bin/sh"          # any existing executable stands in for opencode_cmd
CDIR = os.path.realpath(DIR)
STARTED = 1010.0 - 70   # nav clock (NOW) - the fake's session age (AGE + 10)


class PaneWorld(World):
    """World + `new-pane` (prints terminal_<N>), pane titles and floating flags in list-panes."""

    def __init__(self):
        super().__init__()
        self.extra = {}              # pane id -> {"title", "floating"}
        self.new_pane_drop = False   # print an id, create nothing (zellij with an unknown --tab-id)
        self.new_pane_cwd = None     # override the new pane's reported cwd
        self.new_pane_tab = None     # override the tab it lands in
        self.new_pane_out = None
        self.new_pane_args = []

    def zellij(self, argv, env):
        if argv[1] == "--session" and argv[4] == "new-pane":
            d = env.get("ZELLIJ_SOCKET_DIR", DIR)
            s = self.sessions[(d, argv[2])]
            if s["clients"] == 0:
                raise AssertionError("new-pane with 0 clients")
            args = argv[5:]
            self.new_pane_args.append(args)
            new_id = 300 + len(self.new_pane_args)
            if self.new_pane_out is not None:
                return R(argv, out=self.new_pane_out)
            tab_arg = int(args[args.index("--tab-id") + 1]) if "--tab-id" in args else None
            active = self._active(argv[2], s)
            tab = self.new_pane_tab if self.new_pane_tab is not None else (active if tab_arg is None else tab_arg)
            # zellij 0.45.1: an unknown --tab-id still exits 0 and prints an id; nothing appears
            if not self.new_pane_drop and any(t == tab for t, _n in s["tabs"]):
                cwd = self.new_pane_cwd or args[args.index("--cwd") + 1]
                s["panes"].append((new_id, tab, cwd, False))
                floating = "--floating" in args
                self.extra[new_id] = {"title": args[args.index("--name") + 1], "floating": floating}
                if floating:
                    self.floating.add(new_id)
                # a plain new-pane takes the focus; a --tab-id pane never does (floating in the
                # active tab excepted)
                if tab_arg is None or (floating and tab == active):
                    self._focus(argv[2], s, new_id)
            return R(argv, out=f"terminal_{new_id}\n")
        r = super().zellij(argv, env)
        if argv[1] == "--session" and argv[4] == "list-panes" and r.ok:
            panes = json.loads(r.stdout)
            for p in panes:
                x = self.extra.get(p["id"])
                if x and not p["is_plugin"]:
                    p["title"], p["is_floating"] = x["title"], x["floating"]
            return R(argv, out=json.dumps(panes))
        return r


def no_wt_tab(w):
    s = w.s("S")
    s["tabs"] = [(0, "main")]
    s["panes"] = [(0, 0, "/r", False)]


def opencode_like():
    """A stand-in for the opencode provider's pane launch (argv = the resolved opencode_cmd, no
    setup, no session id), so the pane / links machinery tests do not depend on the real
    provider module; RealOpencodeAgentTest covers the real one end to end."""
    return FakeProvider(registry.spec("opencode"), session_ids=False, setup=False)


class AgentBase(unittest.TestCase):
    FAKE_OPENCODE = True

    def setUp(self):
        if self.FAKE_OPENCODE:
            inject(self, opencode_like())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(ag.os.path, "isdir", return_value=True),
                  mock.patch.object(ag, "AGENT_WAIT_S", 0.0), mock.patch.object(ag, "VERIFY_S", 0.0),
                  mock.patch("agentree.actions.tabs.VERIFY_S", 0.0)):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = Config(opencode_cmd=OC)
        self.links = FakeLinks()

    def world(self, **kw):
        w0, snap, node = make_world_and_snap(**kw)
        w = PaneWorld()
        w.sessions, w.windows, w.agent_env = w0.sessions, w0.windows, w0.agent_env
        return w, snap, node

    def o(self, w, snap, r, find_agents=None, **kw):
        return ag.new_agent(nav_for(w, **kw), r, snap, cfg=self.cfg, state_dir=self.tmp.name, links=self.links,
                            find_agents=find_agents or (lambda s, ids: []))


class NewAgentTest(AgentBase):
    def test_existing_tab(self):
        w, snap, node = self.world()
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER",
                     find_agents=lambda s, ids: [777] if ids == {301} else [])
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › x › opencode")
        self.assertEqual(w.acts(), [
            ("zj", "S", "new-pane", "--tab-id", "1", "--cwd", WT.path, "--name", "opencode", "--", OC),
            ("zj", "S", "go-to-tab-by-id", "1"), ("zj", "S", "focus-pane-id", "terminal_301"),
            ("kitty", SOCK, "focus-window", "--match", "id:5")])
        # the tab id validated against a fresh listing, then clients re-queried right before new-pane
        np = w.index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "new-pane")
        self.assertEqual([w.calls[np - k][0][4] for k in (3, 2, 1)], ["list-tabs", "list-panes", "list-clients"])
        self.assertEqual(self.links.by_path[WT.path], {
            "worktreePath": WT.path, "session": "S", "tabName": "x", "tabId": 1, "agentPids": [777],
            "agentPanes": [301], "socketDir": CDIR, "sessionStarted": STARTED})

    def test_tab_id_pane_is_walked_to(self):
        # zellij 0.45.1: a --tab-id pane never gets focus and focus-pane-id is a no-op for tiled
        # panes: go-to-tab-by-id, read back from list-clients, walk with focus-next-pane
        w, snap, node = self.world()
        w.focus_tiled_noop = True
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertEqual(res.message, "S › x › opencode")
        self.assertTrue(res.close)
        self.assertEqual([a[2] for a in w.acts()], ["new-pane", "go-to-tab-by-id", "focus-pane-id",
                                                    "focus-next-pane", "focus-next-pane"])
        self.assertEqual(w.focused["S"], 301)

    def test_active_tab_switch_before_creation_keeps_the_requested_tab(self):
        w, snap, node = self.world()
        orig = w.zellij
        n = {"lc": 0}

        def z(argv, env):
            if argv[1] == "--session" and argv[4] == "list-clients":
                n["lc"] += 1
                if n["lc"] == 2:                      # the pre-create client check: user switches to tab 0
                    w.active["S"] = 0
                    w.focused.pop("S", None)
            return orig(argv, env)
        w.zellij = z
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertIn((301, 1, WT.path, False), w.s("S")["panes"])         # in the requested tab 1
        self.assertEqual((w.active["S"], w.focused["S"]), (1, 301))

    def test_tab_vanishing_before_creation_is_caught_by_verification(self):
        w, snap, node = self.world()
        orig = w.zellij
        n = {"lc": 0}

        def z(argv, env):
            if argv[1] == "--session" and argv[4] == "list-clients":
                n["lc"] += 1
                if n["lc"] == 2:                      # tab 1 closed after it was validated
                    st = w.s("S")
                    st["tabs"] = [t for t in st["tabs"] if t[0] != 1]
                    st["panes"] = [p for p in st["panes"] if p[1] != 1]
            return orig(argv, env)
        w.zellij = z
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("new-pane reported terminal_301 in S tab x, but it is not listed", res.message)
        self.assertNotIn("go-to-tab-by-id", [a[2] for a in w.acts()])

    def test_printed_id_that_existed_before_is_refused(self):
        w, snap, node = self.world()
        w.new_pane_out = "terminal_2\n"
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("new-pane reported terminal_2, which existed before", res.message)
        self.assertNotIn("focus-pane-id", [a[2] for a in w.acts()])

    def test_focus_failure_keeps_popup_open(self):
        w, snap, node = self.world()
        w.focus_tiled_noop = True
        w.unreachable.add(301)                        # e.g. it landed collapsed in a stack
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S",
                     find_agents=lambda s, ids: [777])
        self.assertTrue(res.ok)
        self.assertFalse(res.close)
        self.assertTrue(res.refresh)
        self.assertEqual(res.message, "agent pane created in tab x but focus failed: could not focus pane "
                                      "terminal_301 (stacked or not reachable with focus-next-pane)")
        self.assertEqual(self.links.by_path[WT.path]["agentPanes"], [301])   # still recorded

    def test_session_lock_held_from_validation_to_navigation(self):
        import fcntl
        from agentree.actions.tabs import lock_path
        w, snap, node = self.world()
        orig = w.zellij
        held = {}

        def probe():
            with open(lock_path(self.tmp.name, DIR, "S"), "a") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    return False
                except BlockingIOError:
                    return True

        def z(argv, env):
            if argv[1] == "--session" and argv[4] in ("new-pane", "go-to-tab-by-id", "focus-pane-id"):
                held[argv[4]] = probe()
            return orig(argv, env)
        w.zellij = z
        korig = w.kitty

        def k(argv):
            if argv[4] == "focus-window":
                held["focus-window"] = probe()
            return korig(argv)
        w.kitty = k
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER")
        self.assertTrue(res.ok, res.message)
        # held from validation through zellij navigation; released before the kitty-only focus
        self.assertEqual(held, {"new-pane": True, "go-to-tab-by-id": True, "focus-pane-id": True,
                                "focus-window": False})
        self.assertFalse(probe())                     # released afterwards

    def test_link_record_merges_with_existing(self):
        w, snap, node = self.world()
        self.links.by_path[WT.path] = {"worktreePath": WT.path, "session": "S", "tabName": "x", "tabId": 1,
                                       "agentPids": [4242]}
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual((self.links.by_path[WT.path]["agentPids"], self.links.by_path[WT.path]["agentPanes"]),
                         ([4242], [301]))

    # ---- links.json ownership: another session's record is never taken over unless it is a
    #      verified rename of the action's session
    def rec(self, **kw):
        r = {"worktreePath": WT.path, "session": "OLD", "tabName": "t", "tabId": 9, "agentPids": [5],
             "agentPanes": [7], "note": "keep me"}
        r.update(kw)
        self.links.by_path[WT.path] = r
        return dict(r)

    def o_link(self, w, snap, node):
        return self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S",
                      find_agents=lambda s, ids: [777])

    def test_link_of_another_live_session_is_untouched(self):
        w, snap, node = self.world()
        w.add_session("B", [(0, "t")], [(0, 0, WT.path, False)])
        before = self.rec(session="B", socketDir=CDIR, sessionStarted=STARTED)
        res = self.o_link(w, snap, node)
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › x › opencode (link kept for B; agent not recorded)")
        self.assertEqual(self.links.by_path[WT.path], before)

    def test_link_verified_rename_adds_the_agent(self):
        # OLD is live nowhere, same socket dir, and S started when OLD did: S is OLD renamed
        w, snap, node = self.world()
        self.rec(socketDir=CDIR, sessionStarted=STARTED + 2)
        res = self.o_link(w, snap, node)
        self.assertEqual(res.message, "S › x › opencode")
        self.assertEqual(self.links.by_path[WT.path], {
            "worktreePath": WT.path, "session": "S", "tabName": "x", "tabId": 1, "agentPids": [5, 777],
            "agentPanes": [7, 301], "note": "keep me", "socketDir": CDIR, "sessionStarted": STARTED})
        # a resurrectable (exited) OLD left in the cache is not live: still a rename
        w, snap, node = self.world()
        w.add_session("OLD", exited=True, clients=0)
        self.rec(socketDir=CDIR, sessionStarted=STARTED)
        self.assertEqual(self.o_link(w, snap, node).message, "S › x › opencode")
        self.assertEqual(self.links.by_path[WT.path]["session"], "S")

    def test_link_rename_of_legacy_record_needs_a_running_witness(self):
        w, snap, node = self.world()
        # no socketDir / sessionStarted: a recorded agent that still carries OLD's name and runs
        # in a live pane of S (same socket dir) proves the rename
        w.agent_env[5] = {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": DIR, "ZELLIJ_PANE_ID": "1"}
        w.proc_start[5] = 500.25
        self.rec()
        res = self.o_link(w, snap, node)
        self.assertEqual(res.message, "S › x › opencode")
        self.assertEqual(self.links.by_path[WT.path]["session"], "S")
        self.assertEqual(self.links.by_path[WT.path]["agentPanes"], [7, 301])
        # a witness whose start time is unknown does not count
        w, snap, node = self.world()
        w.agent_env[5] = {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": DIR, "ZELLIJ_PANE_ID": "1"}
        before = self.rec()
        self.assertIn("link kept for OLD; agent not recorded", self.o_link(w, snap, node).message)
        self.assertEqual(self.links.by_path[WT.path], before)

    def test_verify_rename_returns_the_evidence_it_relied_on(self):
        w, snap, node = self.world()
        o = ag.Opened("S", DIR, 1, "x", 301, WT.path, False)
        nav = nav_for(w, current="S")
        ev, why = ag.verify_rename(nav, o, {"session": "OLD", "socketDir": CDIR, "sessionStarted": STARTED,
                                            "agentPids": [5]}, STARTED)
        self.assertEqual((ev, why), (ag.RenameEvidence("OLD", CDIR, STARTED, ()), None))
        w.agent_env[5] = {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": DIR, "ZELLIJ_PANE_ID": "1"}
        w.agent_env[6] = {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": DIR, "ZELLIJ_PANE_ID": "77"}
        w.proc_start.update({5: 500.25, 6: 600.5})
        ev, why = ag.verify_rename(nav, o, {"session": "OLD", "agentPids": [5, 6]}, STARTED)
        self.assertIsNone(why)
        # socket dir from pid 5 (the first), incarnation proven by pid 5 only (6's pane is not in S)
        self.assertEqual(ev, ag.RenameEvidence("OLD", None, None, ((5, 500.25),)))
        ev, why = ag.verify_rename(nav, o, {"session": "B", "agentPids": []}, STARTED)
        self.assertIsNone(ev)
        self.assertIn("socket dir is unknown", why)

    def test_link_unverifiable_rename_is_kept(self):
        cases = {
            "legacy, no witness": {},
            "witness in another dir": {"_env": {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": "/tmp/zellij-alt",
                                                "ZELLIJ_PANE_ID": "1"}},
            "witness pane not in S": {"_env": {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": DIR,
                                               "ZELLIJ_PANE_ID": "55"}},
            "other incarnation": {"socketDir": CDIR, "sessionStarted": STARTED - 600},
            "other socket dir": {"socketDir": "/tmp/zellij-alt", "sessionStarted": STARTED},
            "same name, other socket dir": {"session": "S", "socketDir": "/tmp/zellij-alt"},
            "old name still live": {"socketDir": CDIR, "sessionStarted": STARTED, "_live": True},
        }
        for name, kw in cases.items():
            with self.subTest(name):
                w, snap, node = self.world()
                env = kw.pop("_env", None)
                if env:
                    w.agent_env[5] = env
                if kw.pop("_live", False):
                    w.add_session("OLD", [(0, "t")], [(0, 0, "/r", False)])
                before = self.rec(**kw)
                res = self.o_link(w, snap, node)
                self.assertTrue(res.ok, res.message)
                self.assertIn(f"link kept for {before['session']}; agent not recorded", res.message)
                self.assertEqual(self.links.by_path[WT.path], before)

    def concurrently(self, change):
        """Run `change(record)` on the stored record between verify_rename and the merge."""
        orig = self.links.merge_link

        def merge(path, mutate):
            change(self.links.by_path[path])
            return orig(path, mutate)
        self.links.merge_link = merge

    def assert_not_adopted(self, res, before_change):
        self.assertIn("link changed concurrently; agent not recorded", res.message)
        rec = self.links.by_path[WT.path]
        self.assertNotEqual(rec.get("session"), "S")
        self.assertEqual(rec["agentPanes"], before_change["agentPanes"])
        self.assertEqual(rec["agentPids"], before_change["agentPids"])

    def test_link_rename_rechecked_under_the_lock(self):
        # verified as a rename of OLD, but someone rewrote the record for B before the merge
        w, snap, node = self.world()
        before = self.rec(socketDir=CDIR, sessionStarted=STARTED)
        self.concurrently(lambda r: r.update(session="B"))
        res = self.o_link(w, snap, node)
        self.assert_not_adopted(res, before)
        self.assertEqual(self.links.by_path[WT.path]["session"], "B")

    def test_link_rename_sessionstarted_changed_before_merge_is_not_adopted(self):
        # OLD and socketDir unchanged, but the record now describes another incarnation
        w, snap, node = self.world()
        before = self.rec(socketDir=CDIR, sessionStarted=STARTED)
        self.concurrently(lambda r: r.update(sessionStarted=STARTED + 3600))
        res = self.o_link(w, snap, node)
        self.assert_not_adopted(res, before)
        self.assertEqual(self.links.by_path[WT.path]["sessionStarted"], STARTED + 3600)

    def test_link_rename_socketdir_changed_before_merge_is_not_adopted(self):
        w, snap, node = self.world()
        before = self.rec(socketDir=CDIR, sessionStarted=STARTED)
        self.concurrently(lambda r: r.update(socketDir=DIR))     # same dir, different stored value
        self.assert_not_adopted(self.o_link(w, snap, node), before)

    def legacy(self, w):
        w.agent_env[5] = {"ZELLIJ_SESSION_NAME": "OLD", "ZELLIJ_SOCKET_DIR": DIR, "ZELLIJ_PANE_ID": "1"}
        w.proc_start[5] = 500.25
        return self.rec()

    def test_link_rename_witness_set_changed_before_merge_is_not_adopted(self):
        cases = {
            "witness dropped from agentPids": lambda w: (lambda r: r.update(agentPids=[42])),
            "witness replaced by another pid": lambda w: (lambda r: r.update(agentPids=[6])),
            "witness pid reused (other start time)": lambda w: (lambda r: w.proc_start.update({5: 999.0})),
            "witness exited": lambda w: (lambda r: w.proc_start.pop(5)),
        }
        for name, make in cases.items():
            with self.subTest(name):
                w, snap, node = self.world()
                before = self.legacy(w)
                self.links.merge_link = FakeLinks.merge_link.__get__(self.links)
                self.concurrently(make(w))
                res = self.o_link(w, snap, node)
                self.assertIn("link changed concurrently; agent not recorded", res.message)
                rec = self.links.by_path[WT.path]
                self.assertEqual((rec["session"], rec["agentPanes"]), ("OLD", before["agentPanes"]))

    def test_link_rename_witness_still_listed_is_adopted(self):
        # unrelated growth of agentPids between verify and merge does not void the evidence
        w, snap, node = self.world()
        self.legacy(w)
        self.concurrently(lambda r: r.update(agentPids=[5, 6]))
        res = self.o_link(w, snap, node)
        self.assertEqual(res.message, "S › x › opencode")
        self.assertEqual(self.links.by_path[WT.path]["session"], "S")

    def test_no_tab_creates_it_then_the_agent(self):
        w, snap, node = self.world(wt_tab=False)
        no_wt_tab(w)
        res = self.o(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertIn("tab x created", res.message)
        acts = w.acts()
        self.assertEqual(acts[0][:4], ("zj", "S", "new-tab", "--cwd"))
        self.assertEqual(acts[1:], [
            ("zj", "S", "new-pane", "--tab-id", "1", "--cwd", WT.path, "--name", "opencode", "--", OC),
            ("zj", "S", "go-to-tab-by-id", "1"), ("zj", "S", "focus-pane-id", "terminal_301")])

    def test_agent_row_uses_its_worktree(self):
        w, snap, node = self.world()
        a = node.worktrees[1].agents[0]
        res = self.o(w, snap, row("agent", a, "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.new_pane_args[0][:4], ["--tab-id", "1", "--cwd", WT.path])
        self.assertEqual(w.active["S"], 1)
        loose = Agent(pid=1, cwd="/elsewhere", session_name="S", pane_id=9)
        res = self.o(w, snap, row("agent", loose, "S"), current="S")
        self.assertIn("/elsewhere is not inside a known worktree", res.message)

    def test_pane_row_and_orphan_worktree_row(self):
        w, snap, node = self.world()
        res = self.o(w, snap, row("pane", ZPane(1, False, 1, "", "fish", (), WT.path + "/src"), "S"), current="S")
        self.assertTrue(res.ok, res.message)
        # orphan worktree (no session): the session agentree runs in
        orphan = WorktreeNode(WT)
        snap2 = Snapshot(snap.taken_at, snap.sessions, Orphans(worktrees=(WT,)))
        res = self.o(w, snap2, row("worktree", orphan, None), current="S")
        self.assertTrue(res.ok, res.message)
        res = self.o(w, snap2, row("worktree", orphan, None))
        self.assertIn("not running inside zellij", res.message)

    def test_root_checkout_is_allowed(self):
        w, snap, node = self.world()
        res = self.o(w, snap, row("worktree", node.worktrees[0], "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.new_pane_args[0][:4], ["--tab-id", "0", "--cwd", "/r"])
        self.assertIn(("zj", "S", "go-to-tab-by-id", "0"), w.acts())

    def test_multi_client_refused_before_anything(self):
        w, snap, node = self.world(clients=2, wt_tab=False)
        no_wt_tab(w)
        res = self.o(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("2 clients attached to S", res.message)
        self.assertEqual(w.acts(), [])

    def test_client_change_right_before_new_pane(self):
        w, snap, node = self.world()
        orig = w.zellij
        n = {"lc": 0}

        def z(argv, env):
            if argv[1] == "--session" and argv[4] == "list-clients":
                n["lc"] += 1
                if n["lc"] >= 2:
                    w.s("S")["clients"] = 2           # a second client attached meanwhile
            return orig(argv, env)
        w.zellij = z
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("2 clients attached to S", res.message)
        self.assertEqual(w.new_pane_args, [])

    def test_no_client_attaches_first(self):
        w, snap, node = self.world(clients=0, wt_tab=False)
        no_wt_tab(w)
        res = self.o(w, snap, row("worktree", WorktreeNode(WT), "S"), current="OTHER")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.launched, ["S"])
        self.assertEqual(len(w.new_pane_args), 1)

    def test_inventory_incomplete(self):
        w, snap, node = self.world(complete=False)
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("inventory incomplete", res.message)
        self.assertEqual(w.acts(), [])
        w, snap, node = self.world()
        w.s("S")["panes"].append((7, 42, "/x", False))          # fresh listing inconsistent
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("inventory incomplete", res.message)
        self.assertEqual(w.acts(), [])

    def test_verification_failures(self):
        cases = [("new_pane_drop", True, "but it is not listed"),
                 ("new_pane_cwd", "/nonexistent-agentree", "but its cwd is /nonexistent-agentree"),
                 ("new_pane_tab", 0, "but it opened in tab 0, not 1")]
        for attr, val, msg in cases:
            with self.subTest(attr):
                w, snap, node = self.world()
                setattr(w, attr, val)
                res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
                self.assertFalse(res.ok)
                self.assertIn("new-pane reported terminal_301 in S tab x, " + msg, res.message)
                self.assertNotIn("focus-pane-id", [a[2] for a in w.acts()])
                self.assertEqual(self.links.by_path, {})

    def test_no_printed_id_is_refused(self):
        # the printed id is the only identity proof (an unknown --tab-id also prints one): without
        # it nothing is focused or recorded
        w, snap, node = self.world()
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "new-pane":
                return R(argv, out="")
            return r
        w.zellij = z
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("new-pane printed no pane id", res.message)
        self.assertEqual([a[2] for a in w.acts()], ["new-pane"])
        self.assertEqual(self.links.by_path, {})

    def test_tab_switch_after_creation_is_undone_by_navigation(self):
        # the user switches tabs between new-pane and the jump: the pane stays in the requested
        # tab (explicit --tab-id) and go-to-tab-by-id + read-back bring the client to it
        w, snap, node = self.world()
        w.focus_tiled_noop = True
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "new-pane":
                w.active["S"] = 0
                w.focused.pop("S", None)
            return r
        w.zellij = z
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertIn((301, 1, WT.path, False), w.s("S")["panes"])
        self.assertEqual((w.active["S"], w.focused["S"]), (1, 301))
        self.assertEqual([a[2] for a in w.acts()][:3], ["new-pane", "go-to-tab-by-id", "focus-pane-id"])

    def test_tab_no_longer_holds_the_worktree(self):
        w, snap, node = self.world()
        orig = w.zellij
        n = {"lp": 0}

        def z(argv, env):
            if argv[1] == "--session" and argv[4] == "list-panes":
                n["lp"] += 1
                if n["lp"] == 2:                      # after ensure_tab's listing: someone cd'd away
                    s = w.s("S")
                    s["panes"] = [(p, t, "/elsewhere" if t == 1 else c, pl) for p, t, c, pl in s["panes"]]
            return orig(argv, env)
        w.zellij = z
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("no longer holds x", res.message)
        self.assertEqual(w.new_pane_args, [])

    def test_default_provider_from_config(self):
        w, snap, node = self.world()
        self.cfg = Config(opencode_cmd=OC, claude_cmd=OC, default_agent="claude")
        fake_cc = FakeProvider(registry.spec("claude"), session_ids=False, setup=False)
        with registry.override(providers={"opencode": opencode_like(), "claude": fake_cc}):
            res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.new_pane_args[0][-4:], ["--name", "claude", "--", OC])
        self.assertEqual([c[0] for c in fake_cc.calls], ["build_launch"])


class RealOpencodeAgentTest(AgentBase):
    """`o` with the real opencode provider: the pane runs the resolved opencode_cmd."""
    FAKE_OPENCODE = False

    def test_existing_tab(self):
        w, snap, node = self.world()
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER",
                     find_agents=lambda s, ids: [777] if ids == {301} else [])
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › x › opencode")
        self.assertEqual(w.new_pane_args, [["--tab-id", "1", "--cwd", WT.path, "--name", "opencode", "--", OC]])
        self.assertEqual(self.links.by_path[WT.path]["agentPids"], [777])

    def test_dry_run(self):
        w, snap, node = self.world(wt_tab=False)
        no_wt_tab(w)
        res = self.o(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S", dry_run=True)
        self.assertTrue(res.ok)
        self.assertFalse(res.close)
        self.assertEqual(res.message, "dry-run: 2 commands logged")
        dry = [a for a, _e, d in w.calls if d]
        self.assertEqual([a[4] for a in dry], ["new-tab", "new-pane"])
        self.assertEqual(dry[1][5:], ("--tab-id", "<new tab>", "--cwd", WT.path, "--name", "opencode", "--", OC))
        self.assertEqual(len(w.s("S")["tabs"]), 1)
        self.assertEqual(self.links.by_path, {})

    def test_missing_opencode(self):
        w, snap, node = self.world()
        self.cfg = Config(opencode_cmd="/nonexistent/opencode-auto")
        res = self.o(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("opencode command not found", res.message)
        self.assertEqual(w.calls, [])

    def test_wrong_rows(self):
        w, snap, node = self.world()
        res = self.o(w, snap, row("session", node, "S"), current="S")
        self.assertIn("select a worktree, agent or pane", res.message)

    def test_real_actions_wiring(self):
        w, snap, node = self.world()
        acts = RealActions(Config(opencode_cmd=OC, zellij_socket_dirs=(DIR,)), dry_run=True, run_fn=w,
                           environ={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_SOCKET_DIR": DIR},
                           sockets=lambda: [SOCK], state_dir=self.tmp.name, links=self.links,
                           nav_overrides=fake_nav(w, clock=lambda: 1010.0, zellij=ZJ, kitty=KT))
        res = acts.new_agent(row("worktree", node.worktrees[1], "S"), snap)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.message, "dry-run: 1 command logged")
        res = acts.new_agent(row("worktree", node.worktrees[1], "S"), snap, provider="opencode")
        self.assertEqual(res.message, "dry-run: 1 command logged")


class ResolveCommandTest(unittest.TestCase):
    def test_resolve_command(self):
        rc = command.resolve_command
        self.assertEqual(rc("/bin/sh -c 'x y'", "z"), ("/bin/sh", "-c", "x y"))
        self.assertIsNone(rc("/nonexistent/x", "z"))
        self.assertIsNone(rc("definitely-not-a-command-agentree", "z"))
        self.assertTrue(rc("", "sh")[0].endswith("/sh"))
        self.assertIsNone(rc("'unbalanced", "z"))
        self.assertIsNone(rc("/", "z"))                                 # a directory is not a command
        self.assertFalse(hasattr(ag, "resolve_command"))                # lives in providers.command now


class ClaudeAgentTest(AgentBase):
    """`o` with provider "claude" (real provider): the same pane primitive with
    `claude --settings <hooks> --session-id <uuid>`."""
    FAKE_OPENCODE = False

    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp.name, "agentreestate")
        p = mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": self.state, "AGENTREE_SELF": "/opt/agentree"})
        p.start()
        self.addCleanup(p.stop)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.claude = os.path.join(self.bin, "claude")
        with open(self.claude, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.claude, 0o755)
        self.cfg = Config(opencode_cmd=OC, claude_cmd=f"{self.claude} --model opus")
        self.settings = os.path.join(self.state, "claude", "settings.json")

    def c(self, w, snap, r, find_agents=None, **kw):
        return ag.new_agent(nav_for(w, **kw), r, snap, cfg=self.cfg, state_dir=self.tmp.name, links=self.links,
                            find_agents=find_agents, provider="claude")

    def test_launch_argv_settings_and_pids(self):
        w, snap, node = self.world()
        seen = {}

        def finder(_self, s, ids, *, session_id, timeout_s):
            seen["finder"] = (_self.id, session_id)
            return (888,) if ids == frozenset({301}) else ()
        prov = registry.get("claude")
        with mock.patch.object(type(prov), "find_launched_pids", autospec=True, side_effect=finder):
            res = self.c(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › x › claude")
        args = tuple(w.new_pane_args[0])
        self.assertEqual(args[:7], ("--tab-id", "1", "--cwd", WT.path, "--name", "claude", "--"))
        cmd = args[7:]
        self.assertEqual(cmd[:5], (self.claude, "--model", "opus", "--settings", self.settings))
        self.assertEqual(cmd[5], "--session-id")
        self.assertRegex(cmd[6], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertEqual(len(cmd), 7)                                   # no prompt
        self.assertEqual(seen["finder"], ("claude", cmd[6]))           # pid poll keyed by our session id
        self.assertEqual(self.links.by_path[WT.path]["agentPids"], [888])
        with open(self.settings) as fh:
            doc = json.load(fh)
        hook = doc["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(hook, "/opt/agentree claude-hook")
        self.assertEqual(sorted(doc["hooks"]), sorted(("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                                                       "PermissionRequest", "Notification", "Stop", "SessionEnd")))

    def test_dry_run_writes_nothing(self):
        w, snap, node = self.world()
        res = self.c(w, snap, row("worktree", node.worktrees[1], "S"), current="S", dry_run=True)
        self.assertTrue(res.ok, res.message)
        self.assertFalse(os.path.exists(self.settings))                 # runner.internal: skipped
        dry = [a for a, _e, d in w.calls if d]
        self.assertEqual([a[4] for a in dry], ["new-pane"])
        self.assertIn("--settings", dry[0])
        self.assertIn("--session-id", dry[0])
        self.assertEqual(dry[0][dry[0].index("--name") + 1], "claude")
        self.assertEqual(self.links.by_path, {})
        with open(os.path.join(self.state, "log")) as fh:
            self.assertIn("DRY", fh.read().split("write claude hook settings")[0].splitlines()[-1])

    def test_missing_claude(self):
        w, snap, node = self.world()
        self.cfg = Config(claude_cmd="definitely-not-claude-agentree")
        res = self.c(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("claude command not found: 'definitely-not-claude-agentree'", res.message)
        self.assertIn("claude_cmd", res.message)
        self.assertEqual(w.calls, [])
        self.assertFalse(os.path.exists(self.settings))

    def test_unknown_provider_is_refused(self):
        w, snap, node = self.world()
        res = ag.new_agent(nav_for(w, current="S"), row("worktree", node.worktrees[1], "S"), snap, cfg=self.cfg,
                           state_dir=self.tmp.name, links=self.links, provider="gpt")
        self.assertFalse(res.ok)
        self.assertIn("unknown agent provider 'gpt'", res.message)
        self.assertEqual(w.calls, [])

    def test_real_actions_wiring(self):
        w, snap, node = self.world()
        acts = RealActions(replace_cfg(self.cfg, zellij_socket_dirs=(DIR,)), dry_run=True, run_fn=w,
                           environ={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_SOCKET_DIR": DIR},
                           sockets=lambda: [SOCK], state_dir=self.tmp.name, links=self.links,
                           nav_overrides=fake_nav(w, clock=lambda: 1010.0, zellij=ZJ, kitty=KT))
        res = acts.new_agent(row("worktree", node.worktrees[1], "S"), snap, provider="claude")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.message, "dry-run: 2 commands logged")     # settings write + new-pane
        self.assertEqual([a[4] for a, _e, d in w.calls if d], ["new-pane"])
        self.assertFalse(os.path.exists(self.settings))


def replace_cfg(cfg, **kw):
    import dataclasses
    return dataclasses.replace(cfg, **kw)


class ResolveClaudeTest(unittest.TestCase):
    """Launch-command lookup through the providers (Provider.resolve_command)."""

    def test_extra_dirs(self):
        cc = registry.get("claude")
        with tempfile.TemporaryDirectory() as home:
            local = os.path.join(home, ".local", "bin")
            os.makedirs(local)
            exe = os.path.join(local, "claude")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            with mock.patch.dict(os.environ, {"HOME": home, "PATH": "/nonexistent"}), \
                    mock.patch("agentree.actions.exec.which", return_value=None):
                self.assertEqual(cc.resolve_command(Config()), (exe,))
                self.assertEqual(cc.resolve_command(Config(claude_cmd="claude --verbose")), (exe, "--verbose"))
                os.chmod(exe, 0o644)                                   # not executable: not found
                self.assertIsNone(cc.resolve_command(Config()))
                cl = os.path.join(home, ".claude", "local")
                os.makedirs(cl)
                exe2 = os.path.join(cl, "claude")
                with open(exe2, "w") as fh:
                    fh.write("#!/bin/sh\n")
                os.chmod(exe2, 0o755)
                self.assertEqual(cc.resolve_command(Config()), (exe2,))
        self.assertEqual(registry.get("opencode").resolve_command(Config(opencode_cmd="/bin/sh -x")), ("/bin/sh", "-x"))
        self.assertIn("set opencode_cmd", registry.missing_command_message("opencode", Config()))


# --------------------------------------------------------------------------- a third provider

AIDER = ProviderSpec(id="aider", label="Aider", module="tests._fake_provider_absent_module",
                     command_key="aider_cmd", default_command="aider")


class FakeThirdProviderAgentTest(AgentBase):
    """A provider injected without core edits: `o` launches it, runs its setup steps, polls its
    pids by the launch's session id, and agent_options offers it."""
    FAKE_OPENCODE = False

    def setUp(self):
        super().setUp()
        self.codex = FakeProvider(pids=[4711])
        inject(self, self.codex)
        self.cfg = Config(opencode_cmd=OC, provider_commands={"codex": f"{OC} --full-auto"})
        self.state = os.path.join(self.tmp.name, "st")

    def x(self, w, snap, r, provider="codex", **kw):
        return ag.new_agent(nav_for(w, **kw), r, snap, cfg=self.cfg, state_dir=self.state, links=self.links,
                            provider=provider)

    def test_pane_launch_setup_and_pids(self):
        w, snap, node = self.world()
        res = self.x(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › x › codex")
        self.assertEqual(w.new_pane_args, [["--tab-id", "1", "--cwd", WT.path, "--name", "codex", "--",
                                            OC, "--full-auto", "--session", "codex-sid-1"]])
        req = self.codex.calls[0][1]
        self.assertEqual((req.mode, req.prompt, req.state_dir), ("pane", "", self.state))
        setup = [c for c in self.codex.calls if c[0] == "execute_setup"]
        self.assertEqual([(c[1].operation, c[2]) for c in setup], [("write_settings", self.state)])
        self.assertTrue(os.path.exists(os.path.join(self.state, "codex", "settings.json")))
        finds = [c for c in self.codex.calls if c[0] == "find_launched_pids"]
        self.assertEqual(finds[0][1:4], ("S", frozenset({301}), "codex-sid-1"))
        self.assertEqual(self.links.by_path[WT.path]["agentPids"], [4711])
        # setup ran before the pane was created
        order = [c[0] for c in self.codex.calls]
        self.assertEqual(order[:2], ["build_launch", "execute_setup"])

    def test_default_provider_and_dry_run(self):
        w, snap, node = self.world()
        self.cfg = Config(opencode_cmd=OC, provider_commands={"codex": OC}, default_agent="codex")
        res = self.x(w, snap, row("worktree", node.worktrees[1], "S"), provider=None, current="S", dry_run=True)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.message, "dry-run: 2 commands logged")      # setup (internal) + new-pane
        dry = [a for a, _e, d in w.calls if d]
        self.assertEqual(dry[0][dry[0].index("--name") + 1], "codex")
        self.assertFalse(os.path.exists(os.path.join(self.state, "codex", "settings.json")))

    def test_unavailable_is_refused_before_anything(self):
        w, snap, node = self.world()
        self.cfg = Config(opencode_cmd=OC, provider_commands={"codex": "/nonexistent/codex"})
        res = self.x(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertEqual(res.message, "codex command not found: '/nonexistent/codex' (set codex_cmd in "
                                      "~/.config/agentree/config.toml)")
        self.assertEqual(w.calls, [])
        self.assertEqual(self.codex.calls, [])

    def test_agent_options(self):
        aider = FakeProvider(AIDER)
        with registry.override(specs=(CODEX, AIDER), providers={"codex": self.codex, "aider": aider}):
            acts = RealActions(self.cfg, dry_run=True, state_dir=self.tmp.name)
            self.assertEqual(acts.agent_options(), AgentOptions(
                choices=(AgentChoice("codex", "Codex", True), AgentChoice("aider", "Aider", False)),
                default_provider="codex"))
            acts = RealActions(replace_cfg(self.cfg, default_agent="aider"), dry_run=True, state_dir=self.tmp.name)
            self.assertEqual(acts.agent_options().default_provider, "aider")
        # with the real descriptors: every registered provider, registry order, the fake last
        ids = [c.id for c in ag.agent_options(self.cfg).choices]
        self.assertEqual(ids, ["opencode", "claude", "codex"])
        self.assertEqual(ag.agent_options(self.cfg).default_provider, "opencode")

    def test_broken_provider_is_offered_unavailable(self):
        bad = ProviderSpec(id="broken", label="Broken", module="tests._no_such_provider_module",
                           command_key="broken_cmd", default_command="broken")
        with registry.override(specs=(CODEX, bad), providers={"codex": self.codex}):
            self.assertEqual(ag.agent_choices(self.cfg),
                             (AgentChoice("codex", "Codex", True), AgentChoice("broken", "Broken", False)))


if __name__ == "__main__":
    unittest.main()
