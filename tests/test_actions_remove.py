"""Remove (worktree / agent / session) and rename: plan + execute with real git and fake zellij."""
import os
import unittest
from dataclasses import replace

from unittest import mock

try:
    from tests.test_actions_jump import DIR, DIR2, KT, NOW, TAKEN, ZJ, fake_nav, row, session
    from tests.test_actions_spawn import SpawnBase, git
    from tests._fake_provider import FakeProvider, inject
except ImportError:  # pragma: no cover
    from test_actions_jump import DIR, DIR2, KT, NOW, TAKEN, ZJ, fake_nav, row, session  # type: ignore
    from test_actions_spawn import SpawnBase, git  # type: ignore
    from _fake_provider import FakeProvider, inject  # type: ignore

from agentree.actions import remove as rm
from agentree.actions import rename as rn
from agentree.actions.api import ActionResult, Plan
from agentree.types import Agent, Orphans, SessionNode, Snapshot, Worktree, WorktreeNode, ZPane, ZTab

START = 1_700_000_000.5


def pane(pid, tab, cwd, plugin=False):
    return ZPane(pid, plugin, tab, "fish", "fish", ("fish",), cwd)


class RemoveBase(SpawnBase):
    def setUp(self):
        super().setUp()
        assert self.wt == os.path.join(self.repo, ".worktrees", "feat-x")
        git("worktree", "add", "-q", "-b", "agent/feat-x", self.wt, "HEAD", cwd=self.repo)
        sub = os.path.join(self.wt, "sub")
        os.makedirs(sub)
        # S: tab 0 main (repo), tab 1 x (all panes in wt), tab 2 mixed (wt + repo)
        self.w.s("S")["tabs"] = [(0, "main"), (1, "x"), (2, "mixed")]
        self.w.s("S")["panes"] = [(0, 0, self.repo, False), (1, 1, self.wt, False), (2, 1, sub, False),
                                  (3, 2, self.wt, False), (4, 2, self.repo, False), (9, 1, None, True)]
        tabs = (ZTab(0, 0, "main", False, (pane(0, 0, self.repo),)),
                ZTab(1, 1, "x", True, (pane(1, 1, self.wt), pane(2, 1, sub), pane(9, 1, None, True))),
                ZTab(2, 2, "mixed", False, (pane(3, 2, self.wt), pane(4, 2, self.repo))))
        self.agent = Agent(pid=4242, cwd=self.wt, meta={"port": 5000}, session_name="S", pane_id=1, start_time=START)
        root = Worktree(self.repo, self.repo, "main", self.base, True)
        wtw = Worktree(self.wt, self.repo, "agent/feat-x", self.base, False)
        self.wnode = WorktreeNode(wtw, tabs=(tabs[1], tabs[2]), agents=(self.agent,))
        self.node = SessionNode(session("S", tabs=tabs), (WorktreeNode(root, tabs=(tabs[0],)), self.wnode))
        self.snap = Snapshot(TAKEN, (self.node,), Orphans())
        self.procs = {4242: {"start": START, "argv": ("/nix/opencode", "--port", "5000"),
                             "env": {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "1"}, "cwd": self.wt}}

    def identity(self, pid):
        return self.procs.get(pid)

    def plan_rm(self, r, **kw):
        return rm.plan_remove(r, self.snap, nav=self.nav(**kw), identity=self.identity)

    def exec_rm(self, plan, typed=None, **kw):
        return rm.execute_remove(plan, typed, self.nav(**kw), identity=self.identity, progress=self.events.append,
                                 links=self.links)


class RemoveWorktreeTest(RemoveBase):
    def wt_row(self):
        return row("worktree", self.wnode, "S")

    def test_root_refused_and_nothing_rows(self):
        res = self.plan_rm(row("worktree", self.node.worktrees[0], "S"))
        self.assertIsInstance(res, ActionResult)
        self.assertIn("root checkout", res.message)
        for kind in ("pane", "group", "footer"):
            self.assertEqual(self.plan_rm(row(kind, None, "S")).message, "nothing to remove")

    def test_clean_merged_plan_and_execute(self):
        plan = self.plan_rm(self.wt_row())
        self.assertIsInstance(plan, Plan)
        self.assertIsNone(plan.confirm.require_text)
        self.assertEqual(plan.steps, (
            (ZJ, "--session", "S", "action", "close-tab-by-id", "1"),
            ("kill", "-TERM", "4242"),
            ("git", "-C", self.repo, "worktree", "remove", self.wt),
            ("git", "-C", self.repo, "update-ref", "-d", "refs/heads/agent/feat-x", self.base),
            ("drop its links.json record",)))
        self.assertIn("tab mixed in S also has panes elsewhere: left open", plan.warnings)
        self.assertIn(f"branch agent/feat-x ({self.base[:10]}) is merged into origin/main ({self.base[:10]}); "
                      "it will be deleted:", plan.confirm.lines)
        self.assertIn(f"$ git -C {self.repo} update-ref -d refs/heads/agent/feat-x {self.base}", plan.confirm.lines)
        res = self.exec_rm(plan)
        self.assertTrue(res.ok and res.refresh, res.message)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        self.assertEqual(self.w.killed, [4242])
        self.assertEqual([t for t, _ in self.w.s("S")["tabs"]], [0, 2])
        self.assertEqual(self.links.removed, [self.wt])

    def test_unmerged_branch_kept(self):
        git("commit", "-q", "--allow-empty", "-m", "work", cwd=self.wt)
        plan = self.plan_rm(self.wt_row())
        self.assertNotIn("branch", [s[3] for s in plan.steps if len(s) > 3])
        self.assertIn("is not merged", " ".join(plan.confirm.lines))
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertTrue(self.branch_exists())

    def test_dirty_requires_branch_name(self):
        with open(os.path.join(self.wt, "README"), "a") as fh:
            fh.write("change\n")
        plan = self.plan_rm(self.wt_row())
        self.assertEqual(plan.confirm.require_text, "agent/feat-x")
        self.assertIn("DIRTY: 1 changed file", plan.confirm.title)
        self.assertIn("--force", plan.steps[2])
        res = self.exec_rm(plan, typed="feat-x")
        self.assertFalse(res.ok)
        self.assertIn("does not match", res.message)
        self.assertTrue(os.path.exists(self.wt))
        self.assertEqual(self.w.killed, [])
        res = self.exec_rm(plan, typed="agent/feat-x")
        self.assertTrue(res.ok, res.message)
        self.assertFalse(os.path.exists(self.wt))

    def test_became_dirty_after_confirm(self):
        plan = self.plan_rm(self.wt_row())
        with open(os.path.join(self.wt, "new.txt"), "w") as fh:
            fh.write("x\n")
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("new changes", res.message)
        self.assertTrue(os.path.exists(self.wt))
        self.assertEqual(self.w.acts(), [])

    def test_agent_identity_and_tab_revalidated_at_execute(self):
        plan = self.plan_rm(self.wt_row())
        self.procs[4242] = dict(self.procs[4242], start=START + 7)      # pid reused
        self.w.s("S")["panes"].append((7, 1, self.repo, False))          # tab x now also holds the repo
        res = self.exec_rm(plan)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.w.killed, [])
        self.assertIn("another process", res.message)
        self.assertNotIn("close-tab-by-id", [a[2] for a in self.w.acts() if a[0] == "zj"])
        skipped = [e for e in self.events if e.state == "skipped"]
        self.assertEqual(len(skipped), 2)

    def test_dry_run(self):
        plan = self.plan_rm(self.wt_row())
        res = self.exec_rm(plan, dry_run=True)
        self.assertTrue(res.ok)
        self.assertEqual(res.message, "dry-run: 5 commands logged")
        self.assertTrue(os.path.exists(self.wt))
        self.assertEqual(self.w.killed, [])
        self.assertEqual(self.links.removed, [])


class BranchIdentityTest(RemoveBase):
    """Finding 2: what was approved is what gets removed."""

    def wt_row(self):
        return row("worktree", self.wnode, "S")

    def test_tip_moved_after_confirm_refused(self):
        plan = self.plan_rm(self.wt_row())
        git("commit", "-q", "--allow-empty", "-m", "late", cwd=self.wt)
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("head, tip changed since confirmation", res.message)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertEqual(self.w.killed, [])

    def test_branch_switched_refused(self):
        plan = self.plan_rm(self.wt_row())
        git("checkout", "-q", "-b", "other", cwd=self.wt)
        res = self.exec_rm(plan)
        self.assertIn("branch", res.message)
        self.assertIn("changed since confirmation", res.message)
        self.assertTrue(self.branch_exists())

    def test_default_ref_moved_refused(self):
        plan = self.plan_rm(self.wt_row())
        git("commit", "-q", "--allow-empty", "-m", "upstream", cwd=self.repo)
        git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)
        res = self.exec_rm(plan)
        self.assertIn("default_sha changed since confirmation", res.message)

    def test_path_recreated_refused(self):
        plan = self.plan_rm(self.wt_row())
        git("worktree", "remove", "--force", self.wt, cwd=self.repo)
        res = self.exec_rm(plan)
        self.assertIn("changed since confirmation", res.message)

    def test_branch_delete_is_compare_and_delete(self):
        plan = self.plan_rm(self.wt_row())
        st = [x for x in plan.data["spec"] if x["kind"] == "branch_delete"][0]
        self.assertEqual(st["argv"][-1], self.base)

    def test_not_merged_anymore_at_execute_keeps_branch(self):
        plan = self.plan_rm(self.wt_row())
        ident = dict(plan.data["identity"])
        with mock.patch.object(rm, "worktree_identity", return_value=ident):
            ident2 = dict(ident, tip="0" * 40)            # pretend: approved tip not an ancestor
            plan.data["identity"] = ident2
            with mock.patch.object(rm, "worktree_identity", return_value=ident2):
                res = self.exec_rm(plan)
        self.assertTrue(res.ok, res.message)
        self.assertIn("no longer merged", res.message)
        self.assertTrue(self.branch_exists())


class OwnTabTest(RemoveBase):
    """Finding 3: never close the tab agentree runs in."""

    def test_plan_refuses_when_agentree_pane_in_closing_tab(self):
        res = self.plan_rm(row("worktree", self.wnode, "S"), current="S")
        self.assertIsInstance(res, Plan)                       # no ZELLIJ_PANE_ID: nothing to protect
        nav = self.nav(current="S")
        nav.env = replace(nav.env, pane_id="2")                # agentree runs in pane 2 (tab x)
        res = rm.plan_remove(row("worktree", self.wnode, "S"), self.snap, nav=nav, identity=self.identity)
        self.assertIsInstance(res, ActionResult)
        self.assertIn("agentree is running in a tab of this worktree; open agentree from another tab (Alt w)", res.message)

    def test_agentree_floating_pane_elsewhere_in_that_tab_still_refuses(self):
        t1 = self.node.session.tabs[1]
        t1b = replace(t1, panes=t1.panes + (pane(50, 1, "/elsewhere"),))
        tabs = (self.node.session.tabs[0], t1b, self.node.session.tabs[2])
        node = replace(self.node, session=replace(self.node.session, tabs=tabs))
        snap = Snapshot(TAKEN, (node,), Orphans())
        nav = self.nav(current="S")
        nav.env = replace(nav.env, pane_id="50")
        res = rm.plan_remove(row("worktree", self.wnode, "S"), snap, nav=nav, identity=self.identity)
        self.assertIn("agentree is running in a tab of this worktree", res.message)

    def test_execute_refuses_if_agentree_moved_into_the_tab(self):
        plan = self.plan_rm(row("worktree", self.wnode, "S"), current="S")
        self.w.s("S")["panes"].append((60, 1, self.wt, False))      # agentree popup opened there since
        nav = self.nav(current="S")
        nav.env = replace(nav.env, pane_id="60")
        res = rm.execute_remove(plan, None, nav, identity=self.identity, links=self.links)
        self.assertFalse(res.ok)
        self.assertIn("agentree is running in a tab of this worktree", res.message)
        self.assertTrue(os.path.isdir(self.wt))

    def test_remove_own_session_refused(self):
        res = self.plan_rm(row("session", self.node, "S"), current="S")
        self.assertIn("agentree runs in", res.message)


class CloseTabIdentityTest(RemoveBase):
    """Finding 9: close-tab steps follow the session incarnation, not the name."""

    def test_session_restarted_between_plan_and_execute(self):
        plan = self.plan_rm(row("worktree", self.wnode, "S"))
        self.w.s("S")["age"] = 1
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("restarted", res.message)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertNotIn("close-tab-by-id", [a[2] for a in self.w.acts() if a[0] == "zj"])

    def test_session_moved_socket_dir(self):
        plan = self.plan_rm(row("worktree", self.wnode, "S"))
        self.w.sessions[(DIR2, "S")] = self.w.sessions.pop((DIR, "S"))
        res = self.exec_rm(plan, dirs=(DIR, DIR2))
        self.assertIn(f"now lives in {DIR2}", res.message)
        self.assertTrue(os.path.isdir(self.wt))

    def test_steps_record_identity(self):
        plan = self.plan_rm(row("worktree", self.wnode, "S"))
        st = plan.data["spec"][0]
        self.assertEqual((st["socket_dir"], st["age"]), (DIR, self.node.session.created_age_s))


class StartTimeTest(RemoveBase):
    """Finding 2 (attempt 2): the snapshot pins the agent; the live process never re-pins it."""

    def test_snapshot_start_time_pins_the_process(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        self.assertEqual(plan.data["spec"][0]["agent"]["start"], START)
        a2 = replace(self.agent, start_time=START - 30)       # the pid now runs something newer
        res = self.plan_rm(row("agent", a2, "S"))
        self.assertIn("already exited", res.message)

    def test_unknown_snapshot_start_is_refused_when_live_has_one(self):
        # selected from /old with no snapshot start; pid now a live opencode process in /new
        old = Agent(pid=4242, cwd="/old", session_name=None, pane_id=None, start_time=None)
        self.procs[4242] = dict(self.procs[4242], cwd="/new", env={})
        res = self.plan_rm(row("agent", old, None))
        self.assertIsInstance(res, ActionResult)
        self.assertEqual(res.message, rm.UNKNOWN_IDENTITY)
        res = self.plan_rm(row("worktree", self.wnode, "S"))     # also blocks a worktree remove
        self.assertIsInstance(res, Plan)                          # (snapshot agent has a start)
        wnode = replace(self.wnode, agents=(replace(self.agent, start_time=None),))
        node = replace(self.node, worktrees=(self.node.worktrees[0], wnode))
        snap = Snapshot(TAKEN, (node,), Orphans())
        res = rm.plan_remove(row("worktree", wnode, "S"), snap, nav=self.nav(), identity=self.identity)
        self.assertEqual(res.message, rm.UNKNOWN_IDENTITY)
        self.assertEqual(self.w.killed, [])

    def test_snapshot_start_but_live_unknown_refused(self):
        self.procs[4242]["start"] = None
        res = self.plan_rm(row("agent", self.agent, "S"))
        self.assertEqual(res.message, rm.UNKNOWN_IDENTITY)

    def test_both_unknown_fall_back_to_other_checks(self):
        self.procs[4242]["start"] = None
        plan = self.plan_rm(row("agent", replace(self.agent, start_time=None), "S"))
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertEqual(self.w.killed, [4242])

    def test_orphan_cwd_must_match_at_plan_and_execute(self):
        orphan = Agent(pid=4242, cwd="/old", session_name=None, pane_id=None, start_time=START)
        self.procs[4242] = dict(self.procs[4242], cwd="/new", env={})
        res = self.plan_rm(row("agent", orphan, None))
        self.assertIsInstance(res, ActionResult)
        self.assertIn("now runs in /new, not /old", res.message)
        self.procs[4242]["cwd"] = "/old"
        plan = self.plan_rm(row("agent", orphan, None))
        self.assertTrue(plan.data["spec"][0]["agent"]["orphan"])
        self.procs[4242]["cwd"] = "/new"                          # moved after confirmation
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("runs in another directory", res.message)
        self.assertEqual(self.w.killed, [])

    def test_orphan_argv_must_be_opencode(self):
        orphan = Agent(pid=4242, cwd=self.wt, session_name=None, pane_id=None, start_time=START)
        self.procs[4242] = dict(self.procs[4242], argv=("python3", "x.py"), env={})
        res = self.plan_rm(row("agent", orphan, None))
        self.assertIn("already exited", res.message)

    def test_start_known_only_on_one_side_at_execute_refuses(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        self.procs[4242]["start"] = None
        res = self.exec_rm(plan)
        self.assertIn("cannot confirm it is the same process", res.message)


class RemoveAgentTest(RemoveBase):
    def test_kill_agent(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        self.assertEqual(plan.kind, "kill-agent")
        self.assertFalse(plan.confirm.danger)
        self.assertEqual(plan.steps, (("kill", "-TERM", "4242"),))
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertEqual(self.w.killed, [4242])

    def test_identity_mismatches_refused(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        for change, why in ((dict(start=START + 1), "another process"),
                            (dict(argv=("python3", "x.py")), "no longer opencode"),
                            (dict(env={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "8"}), "another pane")):
            self.procs[4242] = dict(self.procs[4242], **change)
            res = self.exec_rm(plan)
            self.assertFalse(res.ok)
            self.assertIn(why, res.message)
            self.procs[4242] = dict(self.procs[4242], start=START, argv=("/nix/opencode",),
                                    env={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "1"})
        del self.procs[4242]
        self.assertIn("exited", self.exec_rm(plan).message)
        self.assertEqual(self.w.killed, [])
        self.assertIn("already exited", self.plan_rm(row("agent", self.agent, "S")).message)


class RemoveClaudeAgentTest(RemoveBase):
    """Claude Code agents (provider "claude"): identity checks use the claude provider's
    matches_argv (real provider)."""

    CLAUDE = ("/Users/me/.local/bin/claude", "--settings", "/s/settings.json", "--session-id", "sid-1", "fix it")

    def setUp(self):
        super().setUp()
        self.agent = replace(self.agent, provider="claude", meta={}, session_id="sid-1")
        self.wnode = replace(self.wnode, agents=(self.agent,))
        self.node = replace(self.node, worktrees=(self.node.worktrees[0], self.wnode))
        self.snap = Snapshot(TAKEN, (self.node,), Orphans())
        self.procs[4242] = dict(self.procs[4242], argv=self.CLAUDE)

    def test_kill_claude_agent(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        self.assertIsInstance(plan, Plan, getattr(plan, "message", ""))
        self.assertEqual(plan.steps, (("kill", "-TERM", "4242"),))
        self.assertEqual(plan.confirm.lines[1], f"agent in {self.wt} (pane terminal_1 of S)")
        self.assertEqual(plan.data["spec"][0]["agent"]["provider"], "claude")
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertEqual(self.w.killed, [4242])

    def test_claude_record_rejects_opencode_argv_and_reverse(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        self.procs[4242] = dict(self.procs[4242], argv=("/nix/opencode", "--port", "5000"))
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("no longer claude", res.message)
        self.assertEqual(self.w.killed, [])
        # reverse: the snapshot says opencode, the pid runs claude -> not the same agent
        self.procs[4242] = dict(self.procs[4242], argv=self.CLAUDE)
        oc = replace(self.agent, provider="opencode", meta={"port": 5000})
        self.assertIn("already exited", self.plan_rm(row("agent", oc, "S")).message)
        self.procs[4242] = dict(self.procs[4242], argv=("/nix/opencode", "--port", "5000"))
        oc_plan = self.plan_rm(row("agent", oc, "S"))
        self.assertIsInstance(oc_plan, Plan)
        self.procs[4242] = dict(self.procs[4242], argv=self.CLAUDE)
        res = self.exec_rm(oc_plan)
        self.assertFalse(res.ok)
        self.assertIn("no longer opencode", res.message)
        self.assertEqual(self.w.killed, [])
        # a `claude mcp` helper is not an agent
        self.procs[4242] = dict(self.procs[4242], argv=("claude", "mcp", "serve"))
        self.assertIn("already exited", self.plan_rm(row("agent", self.agent, "S")).message)

    def test_worktree_remove_kills_claude_agent(self):
        plan = self.plan_rm(row("worktree", self.wnode, "S"))
        self.assertIn(("kill", "-TERM", "4242"), plan.steps)
        res = self.exec_rm(plan)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.w.killed, [4242])

    def test_legacy_record_without_kind_is_opencode(self):
        rec = {"pid": 4242, "start": START, "pane_id": 1, "session": "S"}
        self.assertEqual(rm.verify_agent(rec, self.identity), "is no longer opencode")
        self.assertIsNone(rm.verify_agent({**rec, "provider": "claude"}, self.identity))
        self.assertIsNone(rm.verify_agent({**rec, "kind": "claude"}, self.identity))     # pre-provider record

    def test_unknown_provider_is_refused(self):
        rec = {"pid": 4242, "start": START, "pane_id": 1, "session": "S"}
        for p in ("gpt", 3):
            self.assertEqual(rm.verify_agent({**rec, "provider": p}, self.identity),
                             f"has an unknown agent provider {p!r}")
        ghost = replace(self.agent, provider="gpt")
        res = self.plan_rm(row("agent", ghost, "S"))
        self.assertIsInstance(res, ActionResult)
        self.assertIn("unknown agent provider 'gpt'", res.message)
        # a worktree holding such an agent is not removed either (its process is not verifiable)
        self.wnode = replace(self.wnode, agents=(ghost,))
        self.node = replace(self.node, worktrees=(self.node.worktrees[0], self.wnode))
        self.snap = Snapshot(TAKEN, (self.node,), Orphans())
        res = self.plan_rm(row("worktree", self.wnode, "S"))
        self.assertIsInstance(res, ActionResult)
        self.assertIn("unknown agent provider 'gpt'", res.message)
        plan = self.plan_rm(row("agent", self.agent, "S"))
        plan.data["spec"][0]["agent"]["provider"] = "gpt"                 # tampered / future plan
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("unknown agent provider", res.message)
        self.assertEqual(self.w.killed, [])


class RemoveFakeProviderAgentTest(RemoveBase):
    """A provider injected without core edits: remove verifies the pid's argv with its
    matches_argv; every identity guard still applies."""

    def setUp(self):
        super().setUp()
        inject(self, FakeProvider())
        self.agent = replace(self.agent, provider="codex", meta={}, session_id="codex-sid-1")
        self.wnode = replace(self.wnode, agents=(self.agent,))
        self.node = replace(self.node, worktrees=(self.node.worktrees[0], self.wnode))
        self.snap = Snapshot(TAKEN, (self.node,), Orphans())
        self.procs[4242] = dict(self.procs[4242], argv=("/opt/bin/codex", "--session", "codex-sid-1"))

    def test_kill_verifies_argv_through_the_provider(self):
        plan = self.plan_rm(row("agent", self.agent, "S"))
        self.assertIsInstance(plan, Plan, getattr(plan, "message", ""))
        self.assertEqual(plan.data["spec"][0]["agent"]["provider"], "codex")
        self.assertEqual(plan.confirm.lines[1], f"agent in {self.wt} (pane terminal_1 of S)")
        self.procs[4242] = dict(self.procs[4242], argv=("/nix/opencode", "--port", "5000"))
        res = self.exec_rm(plan)
        self.assertFalse(res.ok)
        self.assertIn("no longer codex", res.message)
        self.procs[4242] = dict(self.procs[4242], argv=("codex", "--session", "codex-sid-1"), start=START + 1)
        self.assertIn("another process", self.exec_rm(plan).message)    # start time guard still first
        self.procs[4242] = dict(self.procs[4242], start=START)
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertEqual(self.w.killed, [4242])

    def test_not_its_argv_at_plan_time(self):
        self.procs[4242] = dict(self.procs[4242], argv=("codex", "mcp"))
        self.assertIn("already exited", self.plan_rm(row("agent", self.agent, "S")).message)

    def test_worktree_remove_kills_it(self):
        plan = self.plan_rm(row("worktree", self.wnode, "S"))
        self.assertIn(("kill", "-TERM", "4242"), plan.steps)
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertEqual(self.w.killed, [4242])


class RemoveSessionTest(RemoveBase):
    def test_kill_live(self):
        plan = self.plan_rm(row("session", self.node, "S"), current="OTHER")
        self.assertEqual(plan.kind, "kill-session")
        self.assertTrue(plan.confirm.danger)
        self.assertEqual(plan.steps, ((ZJ, "kill-session", "S"),))
        self.assertIn("tab x: 2 pane(s) will die", plan.confirm.lines)
        self.assertIn("agent 4242 (pid 4242) will die", plan.confirm.lines)
        res = self.exec_rm(plan, current="OTHER")
        self.assertTrue(res.ok, res.message)
        self.assertTrue(self.w.s("S")["exited"])
        kill = [c for c in self.w.calls if c[0][1:2] == ("kill-session",)]
        self.assertEqual(kill[0][1], {"ZELLIJ_SOCKET_DIR": DIR})

    def test_delete_exited(self):
        self.w.s("S")["exited"] = True
        node = replace(self.node, session=session("S", state="exited"))
        plan = self.plan_rm(row("session", node, "S"))
        self.assertEqual(plan.steps, ((ZJ, "delete-session", "S"),))
        self.assertTrue(self.exec_rm(plan).ok)
        self.assertNotIn((DIR, "S"), self.w.sessions)

    def test_refusals(self):
        res = self.plan_rm(row("session", self.node, "S"), current="S")
        self.assertIn("agentree runs in", res.message)
        self.w.add_session("S", d=DIR2, tabs=[(0, "t")])
        res = self.plan_rm(row("session", self.node, "S"), dirs=(DIR, DIR2))
        self.assertIn("several socket dirs", res.message)
        del self.w.sessions[(DIR2, "S")]
        plan = self.plan_rm(row("session", self.node, "S"), current="OTHER")
        self.w.s("S")["age"] = 1                                          # restarted since
        res = self.exec_rm(plan, current="OTHER")
        self.assertIn("restarted", res.message)
        self.assertFalse(self.w.s("S")["exited"])


class RenameTest(RemoveBase):
    def rename(self, new, **kw):
        return rn.rename_session(row("session", self.node, "S"), self.snap, new, self.nav(**kw), links=self.links)

    def test_rename(self):
        res = self.rename("S2")
        self.assertTrue(res.ok and res.refresh, res.message)
        self.assertIn((DIR, "S2"), self.w.sessions)
        self.assertEqual(self.links.renamed, [("S", "S2")])
        cmd = [c for c in self.w.calls if c[0][1:2] == ("--session",) and c[0][4] == "rename-session"]
        self.assertEqual(cmd[0][0], (ZJ, "--session", "S", "action", "rename-session", "S2"))

    def test_refusals(self):
        for bad, why in (("a/b", "cannot contain"), ("a b", "cannot contain"), ("", "empty"),
                         ("x" * 120, "too long"), ("S", "nothing to do")):
            res = self.rename(bad)
            self.assertFalse(res.ok)
            self.assertIn(why, res.message)
        self.w.add_session("taken", exited=True)
        self.assertIn("already exists", self.rename("taken").message)
        node = replace(self.node, session=session("S", state="exited"))
        res = rn.rename_session(row("session", node, "S"), self.snap, "Z", self.nav(), links=self.links)
        self.assertIn("cannot rename", res.message)
        self.assertEqual(self.links.renamed, [])

    def test_own_session_rename_closes_and_stays_refused(self):
        from agentree.actions.real import RealActions
        a = RealActions(self.cfg, run_fn=self.w, environ={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_SOCKET_DIR": DIR,
                                                          "ZELLIJ_PANE_ID": "1"},
                        sockets=lambda: [], links=self.links, identity=self.identity,
                        nav_overrides=fake_nav(self.w, clock=lambda: NOW, owned=lambda s: True,
                                                socket_dirs=lambda: [DIR], zellij=ZJ, kitty=KT))
        res = a.rename_session(row("session", self.node, "S"), self.snap, "S2")
        self.assertTrue(res.ok and res.close and res.refresh, res.message)
        self.assertIn("agentree's own session; closing", res.message)
        node2 = replace(self.node, session=replace(self.node.session, name="S2"))
        res = a.plan_remove(row("session", node2, "S2"), Snapshot(NOW, (node2,), Orphans()))
        self.assertIsInstance(res, ActionResult)
        self.assertIn("agentree runs in", res.message)

    def test_links_failure_is_partial_success(self):
        self.links.rename_fail = True
        res = self.rename("S2")
        self.assertTrue(res.ok and res.refresh)
        self.assertIn("links.json not updated", res.message)
        self.assertIn((DIR, "S2"), self.w.sessions)

    def test_dry_run(self):
        res = self.rename("S2", dry_run=True)
        self.assertEqual(res.message, "dry-run: 1 command logged")
        self.assertIn((DIR, "S"), self.w.sessions)


if __name__ == "__main__":
    unittest.main()
