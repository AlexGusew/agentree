import unittest

from agentree.model.link import WorktreeIndex, link
from agentree.model.sample import API, DOT, ENGINE, WEB, WEB_WT, sample_raw, sample_snapshot
from agentree.types import Agent, KittyHost, Repo, Worktree, ZPane, ZSession, ZTab

NOW = 1_800_000_000.0


def wt(path, root, branch, is_root=False, **kw):
    return Worktree(path=path, repo_root=root, branch=branch, head="0" * 40, is_root=is_root, **kw)


def pane(pid, tab_id, cwd, cmd="fish", plugin=False, exited=False):
    return ZPane(pane_id=pid, is_plugin=plugin, tab_id=tab_id, title=cmd, command=cmd, argv=(cmd,), cwd=cwd,
                 exited=exited)


def sess(name, state="attached", tabs=(), current=False):
    return ZSession(name=name, state=state, created_age_s=100, socket_dir=None if state == "exited" else "/tmp/z",
                    clients=1 if state == "attached" else 0, is_current=current, tabs=tuple(tabs))


R = "/p/repo"
REPO = Repo(R, "repo", "origin/main", (
    wt(R, R, "main", True),
    wt(f"{R}/.worktrees/a", R, "agent/a"),
    wt(f"{R}/.worktrees/b", R, "agent/b"),
))


def by_name(snap):
    return {n.session.name: n for n in snap.sessions}


def wpaths(node):
    return [w.worktree.path for w in node.worktrees]


class IndexTest(unittest.TestCase):
    def test_deepest_and_component_wise(self):
        idx = WorktreeIndex([REPO])
        self.assertEqual(idx.containing(f"{R}/.worktrees/a/src").path, f"{R}/.worktrees/a")
        self.assertEqual(idx.containing(f"{R}/src").path, R)
        self.assertEqual(idx.containing(R + "/").path, R)
        self.assertIsNone(idx.containing("/p/repository"))
        self.assertEqual(idx.containing(f"{R}/.worktrees/ab").path, R)
        self.assertIsNone(idx.containing(None))
        self.assertIsNone(idx.containing("/"))


class LinkRulesTest(unittest.TestCase):
    def test_tabs_and_panes(self):
        s = sess("main", tabs=[
            ZTab(5, 1, "two", False, (pane(3, 5, f"{R}/.worktrees/a/x"), pane(9, 5, None, plugin=True))),
            ZTab(4, 0, "one", True, (pane(1, 4, R), pane(2, 4, "/elsewhere"), pane(4, 4, R, exited=True))),
        ])
        snap = link([s], [REPO], [], now=NOW)
        n = snap.sessions[0]
        self.assertEqual(wpaths(n), [R, f"{R}/.worktrees/a", f"{R}/.worktrees/b"])
        root, a, b = n.worktrees
        self.assertEqual([t.name for t in root.tabs], ["one"])
        self.assertEqual([p.pane_id for p in root.panes], [1])        # exited pane skipped
        self.assertEqual([t.name for t in a.tabs], ["two"])
        self.assertEqual([p.pane_id for p in a.panes], [3])
        self.assertEqual(b.tabs, ())                                    # homed, no tab
        self.assertEqual([p.pane_id for p in n.loose_panes], [2])
        self.assertEqual(snap.orphans.worktrees, ())

    def test_agent_pane_not_listed_and_tab_added(self):
        a_path = f"{R}/.worktrees/a"
        s = sess("main", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R), pane(2, 1, R, cmd="opencode")))])
        ag = Agent(pid=10, cwd=a_path, session_name="main", pane_id=2, status="busy", meta={"port": 1})
        snap = link([s], [REPO], [ag], now=NOW)
        n = snap.sessions[0]
        nodes = {w.worktree.path: w for w in n.worktrees}
        self.assertEqual([p.pane_id for p in nodes[R].panes], [1])
        self.assertEqual(nodes[a_path].agents, (ag,))
        self.assertEqual([t.tab_id for t in nodes[a_path].tabs], [1])

    def test_orphan_agents(self):
        s = sess("main", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R),))])
        dead = sess("old", state="exited")
        agents = [
            Agent(pid=1, cwd=R, session_name=None, pane_id=None),
            Agent(pid=2, cwd=R, session_name="ghost", pane_id=3),
            Agent(pid=3, cwd=R, session_name="main", pane_id=None),
            Agent(pid=4, cwd=R, session_name="old", pane_id=1),
            Agent(pid=5, cwd="/tmp", session_name="main", pane_id=7),
        ]
        snap = link([s, dead], [REPO], agents, now=NOW)
        self.assertEqual([a.pid for a in snap.orphans.agents], [1, 2, 3, 4])
        self.assertEqual([a.pid for a in by_name(snap)["main"].loose_agents], [5])

    def test_homing_majority_and_tiebreak(self):
        a, b = f"{R}/.worktrees/a", f"{R}/.worktrees/b"
        x = sess("x", state="detached", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R),))])
        y = sess("y", state="attached", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, a),))])
        snap = link([x, y], [REPO], [], now=NOW)
        # tie 1-1: attached wins
        self.assertIn(b, wpaths(by_name(snap)["y"]))
        self.assertNotIn(b, wpaths(by_name(snap)["x"]))
        # majority: x holds two
        x2 = sess("x", state="detached", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R), pane(2, 1, a)))])
        snap = link([x2, y], [REPO], [], now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["x"]))
        # a appears under both sessions (tabs in both)
        self.assertIn(a, wpaths(by_name(snap)["y"]))
        self.assertIn(a, wpaths(by_name(snap)["x"]))

    def test_homing_name_fallback_live_only(self):
        # an exited session named after the repo never homes anything: worktrees -> orphans,
        # the root checkout is dropped (orphan_roots=False)
        snap = link([sess("repo", state="exited")], [REPO], [], now=NOW)
        self.assertEqual(by_name(snap)["repo"].worktrees, ())
        self.assertEqual([w.branch for w in snap.orphans.worktrees], ["agent/a", "agent/b"])
        snap = link([sess("repo", state="exited")], [REPO], [], now=NOW, orphan_roots=True)
        self.assertEqual([w.branch for w in snap.orphans.worktrees], ["main", "agent/a", "agent/b"])
        snap = link([sess("repo", state="exited"), sess("repo/a", state="detached")], [REPO], [], now=NOW)
        self.assertEqual(len(by_name(snap)["repo/a"].worktrees), 3)
        self.assertEqual(by_name(snap)["repo"].worktrees, ())
        self.assertEqual(snap.orphans.worktrees, ())

    def test_homing_tie_prefers_repo_named_session(self):
        b = f"{R}/.worktrees/b"
        tab = lambda: [ZTab(1, 0, "t", True, (pane(1, 1, R),))]  # noqa: E731
        # 1-1 tie between two attached sessions: the one named after the repo wins over name order
        snap = link([sess("alpha", tabs=tab()), sess("repo", tabs=tab())], [REPO], [], now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["repo"]))
        self.assertNotIn(b, wpaths(by_name(snap)["alpha"]))
        # `<repo>/...` counts too, and beats the attached state preference
        snap = link([sess("alpha", tabs=tab()), sess("repo/x", state="detached", tabs=tab())], [REPO], [], now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["repo/x"]))
        # a prefix that is not a path component does not
        snap = link([sess("alpha", tabs=tab()), sess("repository", state="detached", tabs=tab())], [REPO], [],
                    now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["alpha"]))
        # majority still beats the name
        a = f"{R}/.worktrees/a"
        alpha2 = sess("alpha", state="detached", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R), pane(2, 1, a)))])
        snap = link([alpha2, sess("repo", tabs=tab())], [REPO], [], now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["alpha"]))
        # two named sessions tie: attached > detached, then name
        snap = link([sess("repo/z", tabs=tab()), sess("repo", state="detached", tabs=tab())], [REPO], [], now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["repo/z"]))
        snap = link([sess("repo/z", tabs=tab()), sess("repo/b", tabs=tab())], [REPO], [], now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["repo/b"]))

    def test_exited_sessions_are_session_only(self):
        b = f"{R}/.worktrees/b"
        # an exited session lists tabs (resurrect layout) with panes in the repo: ignored
        ex = sess("old", state="exited", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R), pane(2, 1, "/x")))])
        snap = link([ex, sess("repo/new", state="detached")], [REPO], [], now=NOW)
        old = by_name(snap)["old"]
        self.assertEqual((old.worktrees, old.loose_panes, old.loose_agents), ((), (), ()))
        self.assertEqual(wpaths(by_name(snap)["repo/new"]), [R, f"{R}/.worktrees/a", b])
        # no live candidate at all: worktrees fall through to orphans, roots dropped
        snap = link([ex, sess("other")], [REPO], [], now=NOW)
        self.assertEqual(by_name(snap)["old"].worktrees, ())
        self.assertEqual([w.branch for w in snap.orphans.worktrees], ["agent/a", "agent/b"])
        ex2 = sess("repo", state="exited", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R),))])
        snap = link([ex, ex2], [REPO], [], now=NOW)
        self.assertEqual([n.worktrees for n in snap.sessions], [(), ()])
        self.assertEqual([w.branch for w in snap.orphans.worktrees], ["agent/a", "agent/b"])

    def test_link_record_to_exited_session_orphans_worktree(self):
        a, b = f"{R}/.worktrees/a", f"{R}/.worktrees/b"
        live = sess("repo", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R),))])
        ex = sess("gone", state="exited", tabs=[ZTab(3, 0, "b-tab", True, (pane(1, 3, b),))])
        ag = Agent(pid=7, cwd=b, session_name=None, pane_id=None)
        recs = [{"worktreePath": b, "session": "gone", "tabName": "b-tab", "agentPids": [7]}]
        snap = link([live, ex], [REPO], [ag], links=recs, now=NOW)
        # b is not homed into the live repo session either: the record says it belongs elsewhere
        self.assertEqual(wpaths(by_name(snap)["repo"]), [R, a])
        self.assertEqual(by_name(snap)["gone"].worktrees, ())
        self.assertEqual([w.path for w in snap.orphans.worktrees], [b])
        self.assertEqual([x.pid for x in snap.orphans.agents], [7])
        # ...unless a live session actually has it open in a tab
        live2 = sess("repo", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R), pane(2, 1, b)))])
        snap = link([live2, ex], [REPO], [], links=recs, now=NOW)
        self.assertIn(b, wpaths(by_name(snap)["repo"]))
        self.assertEqual(snap.orphans.worktrees, ())

    def test_orphan_worktrees_and_roots(self):
        snap = link([sess("other")], [REPO], [], now=NOW)
        self.assertEqual([w.branch for w in snap.orphans.worktrees], ["agent/a", "agent/b"])
        snap = link([sess("other")], [REPO], [], now=NOW, orphan_roots=True)
        self.assertEqual([w.branch for w in snap.orphans.worktrees], ["main", "agent/a", "agent/b"])

    def test_links_take_priority(self):
        b = f"{R}/.worktrees/b"
        s1 = sess("s1", tabs=[ZTab(1, 0, "t", True, (pane(1, 1, R),))])
        s2 = sess("s2", state="detached", tabs=[ZTab(7, 0, "home", True, (pane(1, 7, "/x"),)),
                                                 ZTab(8, 1, "b-tab", False, (pane(2, 8, "/y"),))])
        ag = Agent(pid=99, cwd="/somewhere", session_name=None, pane_id=None, status="idle")
        recs = [{"worktreePath": b + "/", "session": "s2", "tabName": "b-tab", "agentPids": [99]},
                {"worktreePath": "/nope", "session": "s2"},
                {"worktreePath": b, "session": "ghost"}]
        snap = link([s1, s2], [REPO], [ag], links=recs, now=NOW)
        n2 = by_name(snap)["s2"]
        self.assertEqual(wpaths(n2), [b])
        node = n2.worktrees[0]
        self.assertTrue(node.via_link)
        self.assertEqual([t.name for t in node.tabs], ["b-tab"])
        self.assertEqual(node.agents, (ag,))
        self.assertNotIn(b, wpaths(by_name(snap)["s1"]))
        self.assertEqual(snap.orphans.agents, ())

    def test_ordering_and_kitty(self):
        sessions = [sess("b", state="detached"), sess("z"), sess("a", state="exited"), sess("c", current=True,
                                                                                            state="detached"),
                    sess("y")]
        hosts = [KittyHost("s", 1, 1, 1, "y"), KittyHost("s", 1, 2, 2, "y", focused=True)]
        snap = link(sessions, [], [], hosts=hosts, now=NOW)
        self.assertEqual([n.session.name for n in snap.sessions], ["c", "y", "z", "b", "a"])
        self.assertTrue(by_name(snap)["y"].kitty.focused)
        self.assertIsNone(by_name(snap)["z"].kitty)


class SampleLinkTest(unittest.TestCase):
    def setUp(self):
        self.snap = sample_snapshot(NOW)
        self.s = by_name(self.snap)

    def test_sessions(self):
        self.assertEqual([n.session.name for n in self.snap.sessions], ["web-app", "dotfiles", "engine", "scratch"])
        self.assertEqual(self.snap.taken_at, NOW)
        self.assertEqual(self.s["web-app"].kitty.session, "web-app")

    def test_web_app(self):
        n = self.s["web-app"]
        self.assertEqual([w.worktree.label for w in n.worktrees],
                         ["main", "feat/sso-login", "fix/flaky-e2e", "chore/session-store", "feat/billing-export"])
        sso = n.worktrees[1]
        self.assertEqual([a.title for a in sso.agents], ["Refactor token refresh", "Write refresh tests"])
        self.assertEqual([p.pane_id for p in sso.panes], [4])        # opencode panes excluded
        self.assertEqual([t.position for t in sso.tabs], [1])
        self.assertEqual([p.command for p in n.worktrees[0].panes], ["nvim", "fish"])
        self.assertEqual([p.command for p in n.worktrees[2].panes], ["nvim"])  # exited pnpm skipped
        self.assertEqual([p.command for p in n.loose_panes], ["htop"])

    def test_other_sessions_and_orphans(self):
        self.assertEqual(self.s["dotfiles"].worktrees[0].agents[0].status, "needs_input")
        self.assertEqual(self.s["engine"].worktrees[1].agents[0].status, "done")
        self.assertEqual(self.s["scratch"].worktrees, ())
        self.assertEqual([w.path for w in self.snap.orphans.worktrees], [f"{API}/.worktrees/rate-limit"])
        self.assertEqual([a.pid for a in self.snap.orphans.agents], [4401])

    def test_raw_is_relinkable(self):
        self.assertEqual(link(**sample_raw(NOW), now=NOW), self.snap)


if __name__ == "__main__":
    unittest.main()
