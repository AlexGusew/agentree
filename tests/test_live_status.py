"""Live agent status: Gatherer.agents (agents-only relink), LiveStatus (provider watcher glue +
cadence), and the run_app hook (poke -> quick refresh, never overlapping, fallback cadence, lifecycle).
Real opencode / claude providers (module fetches patched); see test_provider_contract for a fake
third provider."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from agentree import config, gather as gmod
from agentree.actions.api import NullActions
from agentree.model.sample import sample_raw, sample_snapshot
from agentree.model.snapshot import to_dict
from agentree.providers import claude as ccmod
from agentree.providers import opencode as ocmod
from agentree.providers import registry
from agentree.types import Agent, Snapshot, SourceWarning, UiState
from agentree.ui import app as appmod

NOW = 1_800_000_000.0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDLE_EVERY = ocmod.OpencodeProvider.IDLE_EVERY        # opencode: every event stream connected
CLAUDE_QUICK_EVERY = ccmod.ClaudeProvider.QUICK_EVERY


def oc_agent(pid, port, **kw):
    kw.setdefault("cwd", "/w")
    kw.setdefault("session_name", None)
    kw.setdefault("pane_id", None)
    return ocmod.make_agent(pid=pid, port=port, start_time=kw.pop("start_time", None), **kw)


def cc_agent(**kw):
    return Agent(**{"pid": 2, "cwd": "/w", "session_name": "s", "pane_id": 3, "provider": "claude",
                    "status": "idle", "status_source": "hooks", **kw})


def providers(oc_factory=None, cc_factory=None):
    """The real opencode + claude providers with injected watcher factories."""
    return [ocmod.OpencodeProvider(registry.spec("opencode"), watcher_factory=oc_factory),
            ccmod.ClaudeProvider(registry.spec("claude"), watcher_factory=cc_factory)]


def wait_for(pred, timeout=3.0, step=0.005):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def strip_agents(d):
    """to_dict(snapshot) without agent records / taken_at: what must not change on a quick refresh."""
    d = json.loads(json.dumps(d))
    d.pop("taken_at", None)
    d.pop("warnings", None)                  # checked separately (opencode's are replaced)
    for n in d["sessions"]:
        n["session"].pop("created_age_s")    # relative ages: checked separately (rebased)
        for w in n["worktrees"]:
            w["worktree"].pop("last_commit_age_s")
    for w in d["orphans"]["worktrees"]:
        w.pop("last_commit_age_s")
    for n in d["sessions"]:
        n.pop("loose_agents")
        for w in n["worktrees"]:
            w.pop("agents")
    d["orphans"].pop("agents")
    return d


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": os.path.join(self.tmp.name, "st"),
                                                "AGENTREE_CONFIG": os.path.join(self.tmp.name, "absent.toml")})
        self.env.start()
        gmod._inflight.clear()
        self.g = gmod.Gatherer()
        self.raw = sample_raw(NOW)
        self.cfg = config.Config(source_timeout_s=0.3, git_budget_s=0.3)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def patch(self, target, fn):
        p = mock.patch(target, fn)
        p.start()
        self.addCleanup(p.stop)


# --------------------------------------------------------------------------- gather_agents

class GatherAgentsTest(_Env):
    def setUp(self):
        super().setUp()
        self.oc_calls = []
        self.agents = self.raw["agents"]
        self.patch("agentree.sources.zellij.fetch", lambda *a, **kw: (self.raw["sessions"], [SourceWarning("zellij", "z")]))
        self.patch("agentree.sources.git.fetch", lambda *a, **kw: (self.raw["repos"], [SourceWarning("git", "g")]))
        self.patch("agentree.sources.kitty.fetch", lambda *a, **kw: (self.raw["hosts"], [SourceWarning("kitty", "k")]))
        self.patch("agentree.providers.opencode.fetch", self.oc_fetch)
        self.cc_calls = []
        self.patch("agentree.providers.claude.fetch", self.cc_fetch)
        self.titles = {}
        self.patch("agentree.sources.zellij.fetch_pane_titles", self.fetch_titles)
        self.oc_warn = [SourceWarning("opencode", "first")]

    def oc_fetch(self, timeout, pane_titles=None, pids=None):
        titles = pane_titles() if callable(pane_titles) else pane_titles
        self.oc_calls.append({"pids": pids, "titles": titles})
        return tuple(a for a in self.agents if a.provider == "opencode"), list(self.oc_warn)

    def cc_fetch(self, timeout=1.0, pids=None, **kw):
        self.cc_calls.append({"pids": pids})
        return tuple(a for a in self.agents if a.provider == "claude" and (pids is None or a.pid in pids)), []

    def oc_pids(self, snap):
        return sorted(a.pid for a in gmod.snapshot_agents(snap) if a.provider == "opencode")

    def fetch_titles(self, targets, timeout=1.0):
        self.title_targets = targets
        return dict(self.titles), []

    def full(self, now=NOW):
        return self.g.full(self.cfg, now=now)

    def test_relinks_with_new_status_tree_otherwise_identical(self):
        full = self.full()
        self.assertIsNone(self.oc_calls[0]["pids"])                     # full gather discovers via ps
        busy = next(a for a in self.agents if a.status != "busy")
        self.agents = tuple(replace(a, status="busy", title="new title") if a.pid == busy.pid else a
                            for a in self.agents)
        self.oc_warn = [SourceWarning("opencode", "second")]
        t0 = time.monotonic()
        quick = self.g.agents(self.cfg, full, now=NOW + 5)
        self.assertLess(time.monotonic() - t0, 0.5)
        # only known pids are queried (no ps), each provider only its own agents
        self.assertEqual(sorted(self.oc_calls[-1]["pids"]), self.oc_pids(full))
        self.assertEqual(self.cc_calls[-1]["pids"], [4302])
        got = {a.pid: a for a in gmod.snapshot_agents(quick)}
        self.assertEqual((got[busy.pid].status, got[busy.pid].title), ("busy", "new title"))
        self.assertEqual(strip_agents(to_dict(quick)), strip_agents(to_dict(full)))
        # every other agent unchanged and in the same place
        def placed(s):
            return [(n.session.name, w.worktree.path, a.pid) for n in s.sessions for w in n.worktrees for a in w.agents]
        self.assertEqual(placed(quick), placed(full))
        # opencode warnings replaced in place, the others kept in order
        self.assertEqual([w.message for w in full.warnings], ["z", "g", "first", "k"])
        self.assertEqual([w.message for w in quick.warnings], ["z", "g", "second", "k"])
        self.assertEqual(quick.taken_at, NOW + 5)
        self.assertIn("total", self.g.timings)

    def test_relink_equals_full_link_of_same_inputs(self):
        full = self.full()
        self.agents = tuple(replace(a, status="needs_input") for a in self.agents)
        quick = self.g.agents(self.cfg, full, now=NOW)
        again = self.full()                                             # full gather, same new agents
        self.assertEqual(to_dict(quick), to_dict(again))

    def test_vanished_agent_pane_reappears(self):
        full = self.full()
        gone = gmod.snapshot_agents(full)[0]
        self.agents = tuple(a for a in self.agents if a.pid != gone.pid)
        quick = self.g.agents(self.cfg, full, now=NOW)
        self.assertNotIn(gone.pid, [a.pid for a in gmod.snapshot_agents(quick)])
        self.assertEqual(to_dict(quick), to_dict(self.full()))

    def test_fresh_pane_titles_used(self):
        full = self.full()
        a = next(x for x in gmod.snapshot_agents(full) if x.session_name and x.pane_id is not None)
        self.titles = {(a.session_name, a.pane_id): "OC | renamed"}
        self.g.agents(self.cfg, full, now=NOW)
        titles = self.oc_calls[-1]["titles"]
        self.assertEqual(titles[(a.session_name, a.pane_id)], "OC | renamed")
        live = {s.name for s in self.raw["sessions"] if s.state != "exited"}
        self.assertTrue({n for n, _d in self.title_targets} <= live)
        self.assertIn(a.session_name, {n for n, _d in self.title_targets})

    def test_titles_failure_keeps_known(self):
        full = self.full()
        known = gmod.pane_titles(self.raw["sessions"])
        names = sorted({n for n, _ in known})
        self.patch("agentree.sources.zellij.fetch_pane_titles", lambda targets, timeout=1.0: ({}, names))
        self.g.agents(self.cfg, full, now=NOW)
        self.assertEqual(self.oc_calls[-1]["titles"], known)

    def test_slow_titles_do_not_block(self):
        full = self.full()
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(targets, timeout=1.0):
            release.wait(5)
            return {}, []
        self.patch("agentree.sources.zellij.fetch_pane_titles", slow)
        t0 = time.monotonic()
        self.g.agents(self.cfg, full, now=NOW)
        self.assertLess(time.monotonic() - t0, gmod.TITLES_WAIT_S + 0.2)
        self.assertEqual(self.oc_calls[-1]["titles"], gmod.pane_titles(self.raw["sessions"]))

    def _slow_titles(self, title):
        release = threading.Event()
        self.addCleanup(release.set)
        a = next(x for x in gmod.snapshot_agents(self.g_full) if x.session_name and x.pane_id is not None)

        def slow(targets, timeout=1.0):
            release.wait(5)
            return {(a.session_name, a.pane_id): title}, []
        self.patch("agentree.sources.zellij.fetch_pane_titles", slow)
        return release, (a.session_name, a.pane_id)

    def _never_titles(self):
        self.patch("agentree.sources.zellij.fetch_pane_titles", lambda targets, timeout=1.0: (time.sleep(1), ({}, []))[1])

    def test_late_titles_used_by_next_quick_refresh(self):
        self.g_full = full = self.full()
        release, key = self._slow_titles("OC | late")
        self.g.agents(self.cfg, full, now=NOW)                 # titles too slow: known ones
        self.assertNotEqual(self.oc_calls[-1]["titles"].get(key), "OC | late")
        release.set()
        self.assertTrue(wait_for(lambda: not gmod.inflight()))
        self._never_titles()
        self.g.agents(self.cfg, full, now=NOW)                 # slow again: the late result (same generation) applies
        self.assertEqual(self.oc_calls[-1]["titles"][key], "OC | late")

    def test_late_titles_of_older_generation_ignored(self):
        self.g_full = full = self.full()
        release, key = self._slow_titles("OC | stale")
        self.g.agents(self.cfg, full, now=NOW)                 # generation 1 fetch still running
        gen1 = self.g.raw.generation
        self.full()                                            # generation 2 published meanwhile
        self.assertEqual(self.g.raw.generation, gen1 + 1)
        release.set()                                          # gen-1 result arrives late
        self.assertTrue(wait_for(lambda: not gmod.inflight()))
        self.assertIsNone(self.g.late_titles(self.g.raw.generation))
        self._never_titles()
        self.g.agents(self.cfg, full, now=NOW)
        self.assertNotEqual(self.oc_calls[-1]["titles"].get(key), "OC | stale")
        self.assertEqual(self.oc_calls[-1]["titles"], gmod.pane_titles(self.raw["sessions"]))

    def test_late_titles_cleared_by_new_full_gather(self):
        self.g_full = full = self.full()
        release, key = self._slow_titles("OC | late")
        self.g.agents(self.cfg, full, now=NOW)
        release.set()
        self.assertTrue(wait_for(lambda: not gmod.inflight()))
        self.assertIsNotNone(self.g.late_titles(self.g.raw.generation))
        self.full()
        self.assertIsNone(self.g._late)

    def test_late_titles_only_for_same_session_identity(self):
        self.g_full = full = self.full()
        a = next(x for x in gmod.snapshot_agents(full) if x.session_name and x.pane_id is not None)
        gen = self.g.raw.generation
        # a result for the same name in another socket dir must not be applied
        self.g._store_titles(gmod._Titles(gen, frozenset({(a.session_name, "/other/dir")}),
                                          {(a.session_name, a.pane_id): "OC | other"}, ()))
        self._never_titles()
        self.g.agents(self.cfg, full, now=NOW)
        self.assertNotEqual(self.oc_calls[-1]["titles"].get((a.session_name, a.pane_id)), "OC | other")

    def test_ages_rebased_and_creation_time_constant(self):
        full = self.full()
        s0 = {n.session.name: n.session for n in full.sessions}
        w0 = {w.worktree.path: w.worktree for n in full.sessions for w in n.worktrees}
        created = {n: full.taken_at - s.created_age_s for n, s in s0.items() if s.created_age_s is not None}
        prev = full
        for dt in (3.0, 10.7, 61.2):
            q = self.g.agents(self.cfg, prev, now=NOW + dt)
            self.assertEqual(q.taken_at, NOW + int(dt))
            for n in q.sessions:
                s = n.session
                if s.created_age_s is not None:
                    self.assertEqual(s.created_age_s, s0[s.name].created_age_s + int(dt))
                    self.assertEqual(q.taken_at - s.created_age_s, created[s.name])   # creation time fixed
                for w in n.worktrees:
                    old = w0[w.worktree.path].last_commit_age_s
                    if old is not None:
                        self.assertEqual(w.worktree.last_commit_age_s, old + int(dt))
            # restart detection (jump.check_incarnation): expected age at a later clock is the same
            clock = NOW + 100
            for n in q.sessions:
                if n.session.created_age_s is not None:
                    self.assertEqual(n.session.created_age_s + (clock - q.taken_at),
                                     s0[n.session.name].created_age_s + (clock - full.taken_at))
            prev = q
        self.assertTrue(created)

    def test_two_instances_do_not_share_state(self):
        g2 = gmod.Gatherer()
        full = self.full()
        self.assertIsNotNone(self.g.raw)
        self.assertIsNone(g2.raw)
        n = len(self.oc_calls)
        g2.agents(self.cfg, full, now=NOW)                     # no inventory of its own: full gather
        self.assertIsNone(self.oc_calls[n]["pids"])
        self.assertEqual(g2.raw.generation, 1)
        self.assertEqual(self.g.raw.generation, 1)
        self.g.full(self.cfg, now=NOW)
        self.assertEqual((self.g.raw.generation, g2.raw.generation), (2, 1))
        self.assertNotEqual(self.g._titles_job, g2._titles_job)
        l1, l2 = gmod.LiveStatus(self.cfg), gmod.LiveStatus(self.cfg)
        self.assertIsNot(l1.gatherer, l2.gatherer)

    def test_module_level_gather_then_gather_agents(self):
        self.patch("agentree.gather._DEFAULT", gmod.Gatherer())
        full = gmod.gather(self.cfg, now=NOW)                  # publishes to the default Gatherer only
        self.assertIsNone(self.g.raw)
        self.assertEqual(gmod._DEFAULT.raw.generation, 1)
        quick = gmod.gather_agents(self.cfg, full, now=NOW + 4)
        self.assertEqual(sorted(self.oc_calls[-1]["pids"]), self.oc_pids(full))
        self.assertEqual(quick.taken_at, NOW + 4)
        self.full()                                            # an explicit Gatherer leaves the default alone
        self.assertEqual((self.g.raw.generation, gmod._DEFAULT.raw.generation), (1, 1))

    def test_opencode_failure_returns_prev(self):
        full = self.full()

        def boom(*a, **kw):
            raise RuntimeError("x")
        self.patch("agentree.providers.opencode.fetch", boom)
        self.assertIs(self.g.agents(self.cfg, full), full)

    def test_opencode_busy_returns_prev(self):
        full = self.full()
        release = threading.Event()
        self.addCleanup(release.set)
        self.patch("agentree.providers.opencode.fetch", lambda *a, **kw: (release.wait(5), ((), []))[1])
        t0 = time.monotonic()
        self.assertIs(self.g.agents(self.cfg, full), full)        # overran: abandoned
        self.assertIs(self.g.agents(self.cfg, full), full)        # still running: not relaunched
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(gmod.inflight(), ["opencode"])

    def test_no_raw_inputs_means_full_gather(self):
        prev = Snapshot(taken_at=1.0, stale=True)
        snap = self.g.agents(self.cfg, prev, now=NOW)
        self.assertIsNone(self.oc_calls[0]["pids"])
        self.assertTrue(snap.sessions)

    def test_no_agents_returns_prev(self):
        self.full()
        empty = Snapshot(taken_at=1.0)
        self.assertIs(self.g.agents(self.cfg, empty), empty)

    def test_quick_keeps_claude_agents(self):
        full = self.full()
        self.assertIn(4302, [a.pid for a in gmod.snapshot_agents(full)])
        self.agents = tuple(replace(a, status="idle", status_source="hooks") if a.pid == 4302 else a
                            for a in self.agents)
        quick = self.g.agents(self.cfg, full, now=NOW + 1)
        got = {a.pid: a for a in gmod.snapshot_agents(quick)}
        self.assertEqual((got[4302].provider, got[4302].status), ("claude", "idle"))
        self.assertEqual(sorted(got), sorted(a.pid for a in gmod.snapshot_agents(full)))

    def test_quick_claude_failure_keeps_prev_claude_agents(self):
        full = self.full()

        def boom(*a, **kw):
            raise RuntimeError("cc")
        self.patch("agentree.providers.claude.fetch", boom)
        self.agents = tuple(replace(a, status="busy") for a in self.agents)
        quick = self.g.agents(self.cfg, full, now=NOW + 1)
        self.assertIsNot(quick, full)                              # opencode part refreshed
        got = {a.pid: a for a in gmod.snapshot_agents(quick)}
        prev = {a.pid: a for a in gmod.snapshot_agents(full)}
        self.assertEqual(got[4302], prev[4302])                    # claude agent kept as it was
        self.assertEqual(got[4102].status, "busy")

    def test_quick_claude_overrun_keeps_prev_claude_agents(self):
        full = self.full()
        release = threading.Event()
        self.addCleanup(release.set)
        self.patch("agentree.providers.claude.fetch", lambda *a, **kw: (release.wait(5), ((), []))[1])
        t0 = time.monotonic()
        quick = self.g.agents(self.cfg, full, now=NOW + 1)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertIn(4302, [a.pid for a in gmod.snapshot_agents(quick)])
        self.assertEqual(gmod.inflight(), ["claude"])

    def test_no_claude_agents_no_claude_job(self):
        self.agents = tuple(a for a in self.agents if a.provider != "claude")
        full = self.full()
        n = len(self.cc_calls)                                     # the full gather's discovery
        quick = self.g.agents(self.cfg, full, now=NOW + 1)
        self.assertEqual(len(self.cc_calls), n)
        self.assertIsNot(quick, full)

    def test_only_claude_agents_no_opencode_job(self):
        self.agents = tuple(a for a in self.agents if a.provider == "claude")
        self.oc_warn = [SourceWarning("opencode", "oc warn")]
        full = self.full()
        n = len(self.oc_calls)
        self.agents = tuple(replace(a, status="idle") for a in self.agents)
        quick = self.g.agents(self.cfg, full, now=NOW + 1)
        self.assertEqual(len(self.oc_calls), n)
        self.assertEqual([a.status for a in gmod.snapshot_agents(quick)], ["idle"])
        self.assertIn(("opencode", "oc warn"), [(w.source, w.message) for w in quick.warnings])


class OpencodeKnownPidsTest(unittest.TestCase):
    def test_pids_skip_ps_and_drop_dead(self):
        oc = ocmod
        envs = {10: (("opencode", "--port", "5010"), {}), 11: None, 12: (("/bin/zsh",), {})}
        seen = []

        def fake_agent(pid, argv, env, cwd, port, timeout, pane_title=None, start=None, attach=None):
            seen.append((pid, port))
            return oc_agent(pid, port, cwd=cwd, start_time=start), None
        with mock.patch.object(oc.procmod, "list_procs", side_effect=AssertionError("ps run")), \
                mock.patch.object(oc.procmod, "proc_args_env", side_effect=lambda p: envs[p]), \
                mock.patch.object(oc.procmod, "proc_cwd", return_value={10: "/w"}), \
                mock.patch.object(oc.procmod, "start_times", return_value={10: 5.0}), \
                mock.patch.object(oc, "_agent", side_effect=fake_agent):
            agents, warns = oc.fetch(timeout=0.5, pids=[10, 11, 12, 10])
            self.assertEqual([(a.pid, a.meta["port"], a.start_time) for a in agents], [(10, 5010, 5.0)])
            self.assertEqual(seen, [(10, 5010)])
            self.assertEqual(oc.fetch(timeout=0.5, pids=[11]), ((), []))


class ZellijTitlesTest(unittest.TestCase):
    def test_fetch_pane_titles(self):
        from agentree.actions.exec import Result
        from agentree.sources import zellij as z
        panes = json.dumps([{"id": 1, "is_plugin": False, "title": "OC | x", "tab_id": 0},
                            {"id": 2, "is_plugin": True, "title": "bar", "tab_id": 0},
                            {"id": 3, "is_plugin": False, "title": "", "tab_id": 0}])

        def act(zj, d, name, args, timeout):
            self.assertEqual(args, z.TITLES_ARGS)
            if name == "bad":
                return Result(("z",), 1, "", "err", False, 0.0)
            return Result(("z",), 0, panes, "", False, 0.0)
        with mock.patch.object(z, "_session_action", act):
            got, failed = z.fetch_pane_titles([("a", "/d"), ("bad", "/d"), ("x", None)])
        self.assertEqual(got, {("a", 1): "OC | x"})
        self.assertEqual(failed, ["bad"])
        self.assertEqual(z.fetch_pane_titles([]), ({}, []))


# --------------------------------------------------------------------------- LiveStatus

class FakeWatcher:
    def __init__(self, on_change):
        self.on_change = on_change
        self.updates = []
        self.health: dict[int, bool] = {}
        self.closed = False

    def update(self, agents):
        self.updates.append(sorted(a.pid for a in agents))

    def healthy(self):
        return dict(self.health)

    def close(self):
        self.closed = True


class LiveStatusTest(unittest.TestCase):
    def setUp(self):
        self.snap = sample_snapshot(NOW)
        self.watchers = []
        self.cc_watchers = []

    def factory(self, on_change):
        w = FakeWatcher(on_change)
        self.watchers.append(w)
        return w

    def live(self):
        return gmod.LiveStatus(config.Config(), full=lambda cfg: self.snap,
                               quick=lambda cfg, prev: replace(prev, taken_at=prev.taken_at + 1),
                               providers=providers(self.factory, self.cc_factory))

    def cc_factory(self, on_change):
        w = FakeWatcher(lambda: on_change())
        w.poke = on_change
        self.cc_watchers.append(w)
        return w

    def test_watch_set_follows_refreshes(self):
        live = self.live()
        every = sorted(a.pid for a in gmod.snapshot_agents(self.snap))
        pids = sorted(a.pid for a in gmod.snapshot_agents(self.snap) if a.provider == "opencode")
        self.assertTrue(pids)
        self.assertIn(4302, every)                       # the sample's claude agent
        live.refresh()                                   # before start: remembered
        pokes = []
        live.start(lambda: pokes.append(1))
        w, cw = self.watchers[0], self.cc_watchers[0]
        self.assertEqual(w.updates, [pids])              # SSE: opencode agents only
        self.assertEqual(cw.updates, [[4302]])           # hook watcher: claude agents only
        live.quick(self.snap)
        self.assertEqual(w.updates, [pids, pids])
        w.on_change(4101, "session.status")
        self.assertEqual(pokes, [1])
        cw.poke()                                        # hook status dir changed -> quick refresh
        self.assertEqual(pokes, [1, 1])
        live.close()
        self.assertTrue(w.closed and cw.closed)
        live.refresh()                                   # after close: no watcher use
        self.assertEqual(len(w.updates), 2)
        live.start(lambda: None)                         # late start after close: closed at once
        self.assertTrue(self.watchers[1].closed and self.cc_watchers[1].closed)

    def test_interval(self):
        live = self.live()
        self.assertEqual(live.interval(), live.POLL_EVERY)       # not started
        live.start(lambda: None)
        w = self.watchers[0]
        self.assertEqual(live.interval(), live.POLL_EVERY)       # no snapshot yet: nothing watched
        live.refresh()
        pids = [a.pid for a in gmod.snapshot_agents(self.snap)]
        w.health = {p: True for p in pids}
        self.assertEqual(live.interval(), IDLE_EVERY)            # every agent covered
        w.health[pids[0]] = False
        self.assertEqual(live.interval(), live.POLL_EVERY)       # one stream down
        del w.health[pids[0]]
        self.assertEqual(live.interval(), live.POLL_EVERY)       # one agent not watched at all
        self.assertEqual((ocmod.POLL_S, ocmod.IDLE_S), (live.POLL_EVERY, IDLE_EVERY))   # doctor's texts

    def test_interval_requires_every_agent_covered(self):
        def snap_with(*agents):
            return Snapshot(taken_at=NOW, orphans=sample_snapshot(NOW).orphans.__class__(agents=agents))

        def ag(pid, port):
            return oc_agent(pid, port)
        cases = [
            ((), {}, "poll"),                                            # no agents: nothing watched
            ((ag(1, 5000), ag(2, 5001)), {1: True, 2: True}, "idle"),
            ((ag(1, 5000), ag(2, None)), {1: True}, "poll"),             # missing port
            ((ag(1, 5000), ag(2, 0)), {1: True, 2: True}, "poll"),       # invalid port
            ((ag(1, 5000), ag(2, 70000)), {1: True, 2: True}, "poll"),
            ((ag(1, 5000), ag(2, 5001)), {1: True}, "poll"),             # no watch
            ((ag(1, 5000), ag(2, 5001)), {1: True, 2: False}, "poll"),   # stopped / gave up
            ((ag(1, 5000),), {1: True, 9: False}, "idle"),               # stale extra watch: ignored
        ]
        for agents, health, want in cases:
            with self.subTest(agents=[(a.pid, a.meta["port"]) for a in agents], health=health):
                self.snap = snap_with(*agents)
                live = self.live()
                live.start(lambda: None)
                live.refresh()
                self.watchers[-1].health = dict(health)
                self.assertEqual(live.interval(), IDLE_EVERY if want == "idle" else live.POLL_EVERY)

    def test_interval_with_real_watcher_counts_unwatchable_agents(self):
        stop = threading.Event()
        srv = _sse_server(lambda chunk: (chunk(b'data: {"type":"server.connected"}\n\n'), stop.wait(5)))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(stop.set)
        port = srv.server_address[1]
        good = oc_agent(os.getpid(), port)
        noport = oc_agent(os.getpid() + 100000, None)
        orph = sample_snapshot(NOW).orphans.__class__
        live = gmod.LiveStatus(config.Config(), full=lambda cfg: Snapshot(taken_at=NOW, orphans=orph(agents=(good, noport))),
                               providers=providers())               # the real EventWatcher
        self.addCleanup(live.close)
        live.start(lambda: None)
        live.refresh()
        self.assertTrue(wait_for(lambda: live.watchers["opencode"].healthy() == {good.pid: True}))
        self.assertEqual(live.interval(), live.POLL_EVERY)       # the port-less agent keeps polling on
        live._full = lambda cfg: Snapshot(taken_at=NOW, orphans=orph(agents=(good,)))
        live.refresh()
        self.assertEqual(live.interval(), IDLE_EVERY)

    def test_attach_clients_covered_by_server_stream(self):
        stop = threading.Event()
        conns = []
        srv = _sse_server(lambda chunk: (conns.append(1), chunk(b'data: {"type":"server.connected"}\n\n'), stop.wait(5)))
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(stop.set)
        port = srv.server_address[1]
        tui = oc_agent(os.getpid(), port, session_name="s", pane_id=1, session_id="ses_root")
        att = oc_agent(os.getpid() + 100000, port, session_name="s", pane_id=2, session_id="ses_kid")
        orph = sample_snapshot(NOW).orphans.__class__
        live = gmod.LiveStatus(config.Config(), full=lambda cfg: Snapshot(taken_at=NOW, orphans=orph(agents=(tui, att))),
                               providers=providers())
        self.addCleanup(live.close)
        live.start(lambda: None)
        live.refresh()
        w = live.watchers["opencode"]
        self.assertTrue(wait_for(lambda: w.healthy() == {tui.pid: True, att.pid: True}))
        self.assertEqual(live.interval(), IDLE_EVERY)            # the attach client rides on the server's stream
        self.assertEqual(w.streams(), {port: sorted([tui.pid, att.pid])})
        self.assertEqual(len(conns), 1)

    def test_claude_agents_do_not_force_polling(self):
        oc = oc_agent(1, 5000)
        cc = cc_agent()
        orph = sample_snapshot(NOW).orphans.__class__
        for agents, health in (((oc, cc), {1: True}), ((cc,), {})):
            with self.subTest(agents=[a.pid for a in agents]):
                self.snap = Snapshot(taken_at=NOW, orphans=orph(agents=agents))
                live = self.live()
                live.start(lambda: None)
                live.refresh()
                self.watchers[-1].health = dict(health)
                self.assertEqual(live.interval(), IDLE_EVERY)
                self.assertEqual(self.watchers[-1].updates[-1], [a.pid for a in agents if a.provider == "opencode"])
                self.assertEqual(self.cc_watchers[-1].updates[-1], [2])

    def test_quick_interval(self):
        orph = sample_snapshot(NOW).orphans.__class__

        cc = cc_agent
        oc = oc_agent(1, 5000, status="busy")
        cases = [((oc,), None), ((cc(),), None), ((cc(status="done"),), None),
                 ((cc(status="busy"),), 3.0),                       # Esc interrupt sends no hook
                 ((cc(status="needs_input"),), 3.0),
                 ((cc(status_source="transcript"),), 3.0), ((cc(status_source="registry"),), 3.0),
                 ((cc(status_source=None, status="unknown"),), 3.0)]
        for agents, want in cases:
            with self.subTest(agents=agents):
                self.snap = Snapshot(taken_at=NOW, orphans=orph(agents=agents))
                live = self.live()
                live.refresh()
                self.assertEqual(live.quick_interval(), 3.0 if any(a.provider == "claude" for a in agents) else None)
                live.start(lambda: None)                    # with the hook watcher running
                self.assertEqual(live.quick_interval(), want)
                live.close()

    def test_events_import_is_lazy(self):
        code = ("import sys, agentree.cli, agentree.gather; from agentree.gather import LiveStatus; "
                "from agentree.config import Config; LiveStatus(Config()); "
                "print('agentree.providers._opencode_events' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
        self.assertEqual(r.stdout.strip(), "False")


def _sse_server(script):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(data: bytes):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
            try:
                script(chunk)
            except OSError:
                pass
            self.close_connection = True
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


class LiveStatusRealWatcherTest(unittest.TestCase):
    def test_event_pokes_and_close_leaves_no_threads(self):
        stop = threading.Event()

        def script(chunk):
            chunk(b'data: {"type":"server.connected","properties":{}}\n\n')
            time.sleep(0.1)
            chunk(b'data: {"type":"session.status","properties":{"status":{"type":"busy"}}}\n\n')
            stop.wait(5)
        srv = _sse_server(script)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(stop.set)
        port = srv.server_address[1]
        agent = oc_agent(os.getpid(), port)
        snap = Snapshot(taken_at=NOW, orphans=sample_snapshot(NOW).orphans.__class__(agents=(agent,)))
        quick_calls = []
        live = gmod.LiveStatus(config.Config(), full=lambda cfg: snap,
                               quick=lambda cfg, prev: (quick_calls.append(1), prev)[1])
        pokes = []
        before = {t.ident for t in threading.enumerate()}
        live.start(lambda: pokes.append(time.monotonic()))
        live.refresh()
        self.assertTrue(wait_for(lambda: len(pokes) >= 2))          # server.connected + session.status
        self.assertTrue(wait_for(lambda: live.interval() == IDLE_EVERY))
        stop.set()
        t0 = time.monotonic()
        live.close()
        self.assertLess(time.monotonic() - t0, 0.6)
        self.assertTrue(wait_for(lambda: not [t for t in threading.enumerate()
                                              if t.ident not in before and t.name.startswith("agentree-sse")]))


class ClaudeStatusWatcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": self.tmp.name,
                                           "CLAUDE_CONFIG_DIR": os.path.join(self.tmp.name, "cc")})
        env.start()
        self.addCleanup(env.stop)

    def test_status_dir_change_pokes_and_close_is_prompt(self):
        StatusWatcher = ccmod.StatusWatcher
        pokes = []
        before = {t.ident for t in threading.enumerate()}
        w = StatusWatcher(lambda: pokes.append(1), period=0.02)
        cc = cc_agent(session_name=None, pane_id=None, status="unknown", status_source=None)
        w.update([oc_agent(1, 5000)])
        self.assertFalse(w.active)                                # opencode only: no thread
        self.assertFalse([t for t in threading.enumerate() if t.name == "agentree-claude-status"])
        w.update([cc])
        self.assertTrue(w.active)
        time.sleep(0.1)
        ccmod.handle_hook({"session_id": "s1", "hook_event_name": "UserPromptSubmit"}, {}, 2)
        self.assertTrue(wait_for(lambda: pokes))
        n = len(pokes)
        time.sleep(0.1)
        self.assertEqual(len(pokes), n)                           # no change: no more pokes
        ccmod.handle_hook({"session_id": "s1", "hook_event_name": "Stop"}, {}, 2)
        self.assertTrue(wait_for(lambda: len(pokes) > n))
        t0 = time.monotonic()
        w.close()
        self.assertLess(time.monotonic() - t0, 0.3)
        self.assertTrue(wait_for(lambda: not [t for t in threading.enumerate()
                                              if t.ident not in before and t.name == "agentree-claude-status"]))

    def test_live_status_wires_real_watcher(self):
        cc = cc_agent(session_name=None, pane_id=None)
        snap = Snapshot(taken_at=NOW, orphans=sample_snapshot(NOW).orphans.__class__(agents=(cc,)))
        live = gmod.LiveStatus(config.Config(), full=lambda cfg: snap, quick=lambda cfg, prev: prev,
                               providers=providers(FakeWatcher))    # the real claude StatusWatcher
        pokes = []
        live.start(lambda: pokes.append(1))
        live.refresh()
        self.assertIsNone(live.quick_interval())                  # hooks cover idle claude agents
        live.watchers["claude"].period = 0.02
        time.sleep(0.6)                                           # first stamp taken at thread start
        ccmod.handle_hook({"session_id": "s2", "hook_event_name": "SessionStart"}, {}, 2)
        self.assertTrue(wait_for(lambda: pokes))
        t0 = time.monotonic()
        live.close()
        self.assertLess(time.monotonic() - t0, 0.8)


# --------------------------------------------------------------------------- run_app hook

class FakeLive:
    def __init__(self, interval=10.0, quick_s=0.0):
        self.started = []
        self.poke = None
        self.quick_calls = []
        self.closed = 0
        self._interval = interval
        self.quick_s = quick_s
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def start(self, poke):
        self.started.append(time.monotonic())
        self.poke = poke

    def quick(self, prev):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.quick_calls.append(time.monotonic())
        time.sleep(self.quick_s)
        with self.lock:
            self.active -= 1
        return replace(prev, taken_at=prev.taken_at + 1, stale=False)

    def interval(self):
        return self._interval

    def close(self):
        self.closed += 1


def make_app(live, refresh=None):
    snap = sample_snapshot(NOW)
    app = appmod.App(cfg=config.Config(), snapshot=snap, ui=UiState(), refresh=refresh or (lambda: snap),
                     actions=NullActions(), home="/Users/me", size=(120, 32), live=live)
    app.render()
    return app


def pump(app, until, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.drain_results()
        app.step_timers(time.monotonic())
        if until():
            return True
        time.sleep(0.003)
    return until()


class AppHookTest(unittest.TestCase):
    def test_poke_from_thread_triggers_quick_refresh(self):
        live = FakeLive()
        app = make_app(live)
        threading.Thread(target=app.poke).start()
        self.assertTrue(pump(app, lambda: live.quick_calls and not app.refreshing))
        self.assertEqual(app.st.snapshot.taken_at, NOW + 1)
        self.assertEqual(len(live.quick_calls), 1)

    def test_pokes_coalesced_and_throttled(self):
        live = FakeLive()
        app = make_app(live)
        for _ in range(20):
            app.poke()
        self.assertTrue(pump(app, lambda: len(live.quick_calls) == 1 and not app.refreshing))
        for _ in range(5):
            app.poke()
        self.assertTrue(pump(app, lambda: len(live.quick_calls) == 2 and not app.refreshing))
        self.assertGreaterEqual(live.quick_calls[1] - live.quick_calls[0], appmod.QUICK_GAP - 0.01)
        time.sleep(0.2)
        pump(app, lambda: False, timeout=0.1)
        self.assertEqual(len(live.quick_calls), 2)

    def test_never_overlaps_full_refresh_and_follows_it(self):
        live = FakeLive(quick_s=0.05)
        running = []
        overlap = []

        def full():
            running.append(1)
            if live.active:
                overlap.append(1)
            time.sleep(0.15)
            running.pop()
            return replace(sample_snapshot(NOW), taken_at=NOW + 100)
        app = make_app(live, refresh=full)
        app.next_refresh = time.monotonic() + 100
        app.start_refresh()
        app.poke()
        app.poke()
        self.assertTrue(pump(app, lambda: bool(live.quick_calls) and not app.refreshing))
        self.assertEqual(overlap, [])
        self.assertEqual(live.max_active, 1)
        self.assertEqual(len(live.quick_calls), 1)
        self.assertEqual(app.st.snapshot.taken_at, NOW + 101)          # quick ran on the full result
        # a full refresh requested during a quick one is queued, not overlapped
        app.poke()
        self.assertTrue(pump(app, lambda: app.refreshing))
        app.start_refresh()
        self.assertTrue(app.refresh_again)
        self.assertTrue(pump(app, lambda: not app.refreshing and not app.refresh_again))
        self.assertEqual(overlap, [])

    def test_fallback_cadence(self):
        for interval in (2.0, 10.0):
            live = FakeLive(interval=interval)
            app = make_app(live)
            app.start_refresh()
            self.assertTrue(pump(app, lambda: not app.refreshing))
            self.assertAlmostEqual(app.next_refresh - time.monotonic(), interval, delta=0.2)
            app.poke()
            nr = app.next_refresh
            self.assertTrue(pump(app, lambda: live.quick_calls and not app.refreshing))
            self.assertEqual(app.next_refresh, nr)                     # quick refreshes keep the cadence

    def test_periodic_quick_refresh(self):
        live = FakeLive(interval=100.0)
        live.quick_interval = lambda: 0.1
        app = make_app(live)
        app.start_refresh()
        self.assertTrue(pump(app, lambda: not app.refreshing))
        self.assertIsNotNone(app.next_quick)
        self.assertTrue(pump(app, lambda: len(live.quick_calls) >= 2 and not app.refreshing))
        live.quick_interval = lambda: None                              # e.g. hooks cover every agent now
        n = len(live.quick_calls)
        self.assertTrue(pump(app, lambda: app.next_quick is None and not app.refreshing))
        time.sleep(0.25)
        pump(app, lambda: False, timeout=0.05)
        self.assertLessEqual(len(live.quick_calls), n + 1)
        self.assertIsNone(app.next_quick)

    def test_without_live_unchanged(self):
        app = make_app(None)
        app.poke()                                                     # ignored without a live hook
        app.start_refresh()
        self.assertTrue(pump(app, lambda: not app.refreshing))
        self.assertAlmostEqual(app.next_refresh - time.monotonic(), appmod.REFRESH_EVERY, delta=0.2)
        self.assertIsNone(app.quick_due)


class RunAppLiveTest(unittest.TestCase):
    """run_app on a real pty with a fake live hook."""

    def run_pty(self, live, after_start):
        master, slave = os.openpty()
        stop = threading.Event()
        out = bytearray()

        def drain():
            while not stop.is_set():
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                out.extend(data)

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        driver = threading.Thread(target=after_start, args=(master,), daemon=True)
        before = threading.active_count()
        try:
            driver.start()
            rc = appmod.run_app(cfg=config.Config(), snapshot=sample_snapshot(NOW), ui=UiState(),
                                refresh=lambda: sample_snapshot(NOW), actions=NullActions(),
                                save_ui=lambda ui: None, tty_in=slave, tty_out=slave, live=live)
            driver.join(2)
        finally:
            stop.set()
            os.close(slave)
            os.close(master)
            t.join(1)
        return rc, bytes(out), before

    def test_poke_quick_refresh_then_quit(self):
        live = FakeLive()

        def drive(master):
            assert wait_for(lambda: live.poke is not None)
            live.poke()
            assert wait_for(lambda: bool(live.quick_calls))
            os.write(master, b"q")
        rc, out, before = self.run_pty(live, drive)
        self.assertEqual(rc, 0)
        self.assertEqual(len(live.started), 1)
        self.assertEqual(len(live.quick_calls), 1)
        self.assertEqual(live.closed, 1)
        self.assertIn(b"\x1b[?1049l", out)                     # terminal restored
        self.assertTrue(wait_for(lambda: threading.active_count() <= before))

    def test_start_failure_falls_back_to_polling(self):
        class Broken(FakeLive):
            def start(self, poke):
                raise RuntimeError("no sockets")
        live = Broken()

        def drive(master):
            time.sleep(0.1)
            os.write(master, b"q")
        rc, out, _ = self.run_pty(live, drive)
        self.assertEqual(rc, 0)
        self.assertEqual(live.closed, 1)
        self.assertIn(b"\x1b[?1049l", out)


if __name__ == "__main__":
    unittest.main()
