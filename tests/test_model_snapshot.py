import copy
import json
import unittest
from dataclasses import replace

from agentree.model import fmt
from agentree.model.snapshot import SnapshotFormatError, from_dict, repo_from_dict, repo_to_dict, to_dict
from agentree.types import (Agent, KittyHost, Orphans, Repo, SessionNode, Snapshot, SourceWarning, Worktree,
                       WorktreeNode, ZPane, ZSession, ZTab)

NOW = 1_800_000_000.0
WEB = "/Users/me/Projects/web-app"
SSO = f"{WEB}/.worktrees/sso-login"
API = "/Users/me/Projects/api"
CLAUDE_SID = "0b5e2c4e-7d1a-4f3e-9c2b-5a6d7e8f9a01"


def fixture_repo() -> Repo:
    return Repo(WEB, "web-app", "origin/main", (
        Worktree(WEB, WEB, "main", "a1b2c3d", True, dirty=0, ahead=0, behind=0, base_ref="origin/main",
                 last_commit="Merge pull request #212", last_commit_age_s=7200),
        Worktree(SSO, WEB, "feat/sso-login", "d4e5f60", False, dirty=6, ahead=3, behind=0,
                 base_ref="origin/main", last_commit="Extract token refresh", last_commit_age_s=1500),
    ))


def fixture_snapshot(now: float) -> Snapshot:
    """A self-contained snapshot (sessions, worktrees, agents of every provider kind incl. an
    unknown one, panes, kitty, orphans) that does not depend on agentree.model.sample."""
    repo = fixture_repo()
    main, sso = repo.worktrees
    panes0 = (ZPane(0, False, 0, "nvim", "nvim", ("nvim", "src/app.tsx"), WEB, focused=True),
              ZPane(1, False, 0, "fish", "fish", ("fish",), WEB),
              ZPane(0, True, 0, "tab-bar", None, (), None))
    panes1 = (ZPane(2, False, 1, "opencode", "opencode", ("opencode", "--port", "5000"), SSO),
              ZPane(3, False, 1, "claude", "claude", ("claude", "--session-id", CLAUDE_SID), SSO),
              ZPane(4, False, 1, "fish", "fish", ("fish",), f"{SSO}/src"))
    tabs = (ZTab(0, 0, "web-app", True, panes0), ZTab(1, 1, "sso-login", False, panes1))
    session = ZSession("web-app", "attached", 26 * 3600, "/tmp/zellij-502", 1, True, tabs)
    oc = Agent(pid=4101, cwd=SSO, session_name="web-app", pane_id=2, status="busy", status_since_s=120,
               title="Refactor token refresh", last_message="Moved the refresh logic.",
               context_tokens=12_345, context_limit=200_000, provider="opencode", session_id="ses_a1",
               status_quality="exact", meta={"port": 5000, "api_ok": True})
    cc = Agent(pid=4302, cwd=SSO, session_name="web-app", pane_id=3, status="needs_input",
               status_since_s=45, title="Vectorise", provider="claude", session_id=CLAUDE_SID,
               status_source="hooks", status_quality="exact")
    other = Agent(pid=4500, cwd=SSO, session_name="web-app", pane_id=None, status="idle",
                  provider="codex", session_id="cx-1", status_quality="approximate",
                  status_note="Status is approximate", meta={"model": "o4", "turns": 3, "ratio": 0.5,
                                                             "sandbox": False, "extra": None})
    loose = Agent(pid=4600, cwd="/tmp", session_name="web-app", pane_id=1, provider="opencode",
                  status_quality="unavailable", status_note="Live status unavailable",
                  meta={"port": None, "api_ok": False})
    node = SessionNode(
        session,
        worktrees=(WorktreeNode(main, tabs=(tabs[0],), panes=panes0[:2]),
                   WorktreeNode(sso, tabs=(tabs[1],), agents=(oc, cc, other), panes=(panes1[2],))),
        loose_agents=(loose,),
        kitty=KittyHost("unix:/tmp/kitty.sock-11854", 1, 1, 1, "web-app", focused=True))
    exited = SessionNode(ZSession("scratch", "exited", 3 * 86400, None))
    orphan_wt = Worktree(f"{API}/.worktrees/rate-limit", API, "agent/rate-limit", "0011223", False,
                         dirty=2, ahead=5)
    orphan = Agent(pid=4401, cwd=orphan_wt.path, session_name=None, pane_id=None, status="busy",
                   status_since_s=600, title="Add rate limiter", provider="opencode", session_id="ses_d1",
                   status_quality="exact", meta={"port": 5004, "api_ok": True})
    return Snapshot(taken_at=now, sessions=(node, exited),
                    orphans=Orphans(worktrees=(orphan_wt,), agents=(orphan,)))


def agents_of(snap: Snapshot) -> list[Agent]:
    out: list[Agent] = []
    for n in snap.sessions:
        for w in n.worktrees:
            out.extend(w.agents)
        out.extend(n.loose_agents)
    out.extend(snap.orphans.agents)
    return out


def agent_dicts(d: dict) -> list[dict]:
    out: list[dict] = []
    for n in d["sessions"]:
        for w in n["worktrees"]:
            out.extend(w["agents"])
        out.extend(n["loose_agents"])
    out.extend(d["orphans"]["agents"])
    return out


def with_agents(agents: tuple[Agent, ...]) -> Snapshot:
    return Snapshot(taken_at=NOW, orphans=Orphans(agents=agents))


class SnapshotRoundTripTest(unittest.TestCase):
    def test_round_trip_through_json(self):
        snap = replace(fixture_snapshot(NOW), warnings=(SourceWarning("kitty", "no socket"),))
        d = to_dict(snap)
        text = json.dumps(d, allow_nan=False)
        back = from_dict(json.loads(text))
        self.assertEqual(back, snap)
        self.assertIsInstance(back.sessions, tuple)
        self.assertIsInstance(back.sessions[0].session.tabs[0].panes[0].argv, tuple)
        self.assertEqual([a.meta for a in agents_of(back)], [a.meta for a in agents_of(snap)])

    def test_dict_shape(self):
        d = to_dict(fixture_snapshot(NOW))
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["sessions"][0]["session"]["name"], "web-app")
        self.assertIsInstance(d["sessions"], list)
        self.assertIn("orphans", d)

    def test_stale_and_empty(self):
        snap = Snapshot(taken_at=1.0, stale=True)
        self.assertEqual(from_dict(to_dict(snap)), snap)

    def test_bad_version(self):
        with self.assertRaises(ValueError):
            from_dict({"version": 99, "taken_at": 1})

    def test_unknown_keys_ignored(self):
        d = to_dict(fixture_snapshot(NOW))
        d["sessions"][0]["session"]["future_field"] = 1
        d["sessions"][0]["worktrees"][1]["agents"][0]["future_field"] = {"x": [1]}
        self.assertEqual(from_dict(d), fixture_snapshot(NOW))

    def test_repo_round_trip(self):
        repo = fixture_repo()
        self.assertEqual(repo_from_dict(json.loads(json.dumps(repo_to_dict(repo)))), repo)
        bad = repo_to_dict(repo)
        bad["worktrees"][0]["is_root"] = "true"
        with self.assertRaises(SnapshotFormatError):
            repo_from_dict(bad)


class SnapshotValidationTest(unittest.TestCase):
    """Syntactically valid JSON with the wrong shape must raise SnapshotFormatError."""

    def setUp(self):
        self.good = json.loads(json.dumps(to_dict(fixture_snapshot(NOW))))

    def bad(self, mutate):
        d = copy.deepcopy(self.good)
        mutate(d)
        with self.assertRaises(SnapshotFormatError):
            from_dict(d)

    def test_is_value_error(self):
        self.assertTrue(issubclass(SnapshotFormatError, ValueError))
        with self.assertRaises(SnapshotFormatError):
            from_dict({"version": 99, "taken_at": 1})

    def test_corruptions(self):
        s0 = lambda d: d["sessions"][0]                     # noqa: E731
        a0 = lambda d: s0(d)["worktrees"][1]["agents"][0]   # noqa: E731
        cases = {
            "session name int": lambda d: s0(d)["session"].__setitem__("name", 123),
            "state enum": lambda d: s0(d)["session"].__setitem__("state", "zombie"),
            "agent status enum": lambda d: a0(d).__setitem__("status", "weird"),
            "tabs as dict": lambda d: s0(d)["session"].__setitem__("tabs", {"0": {}}),
            "missing nested field": lambda d: s0(d)["worktrees"][0]["worktree"].__delitem__("path"),
            "missing worktree record": lambda d: s0(d)["worktrees"][0].__delitem__("worktree"),
            "bool as string": lambda d: s0(d)["session"]["tabs"][0].__setitem__("active", "true"),
            "bool as int": lambda d: s0(d)["session"].__setitem__("is_current", 1),
            "int as bool": lambda d: s0(d)["session"]["tabs"][0].__setitem__("tab_id", True),
            "int as float": lambda d: s0(d)["session"]["tabs"][0].__setitem__("position", 1.5),
            "int as string": lambda d: a0(d).__setitem__("pid", "4101"),
            "optional wrong type": lambda d: s0(d)["worktrees"][0]["worktree"].__setitem__("dirty", "3"),
            "argv not array": lambda d: s0(d)["session"]["tabs"][0]["panes"][0].__setitem__("argv", "nvim"),
            "argv item": lambda d: s0(d)["session"]["tabs"][0]["panes"][0].__setitem__("argv", ["nvim", 3]),
            "taken_at string": lambda d: d.__setitem__("taken_at", "x"),
            "taken_at nan": lambda d: d.__setitem__("taken_at", float("nan")),
            "taken_at missing": lambda d: d.__delitem__("taken_at"),
            "stale string": lambda d: d.__setitem__("stale", "no"),
            "sessions object": lambda d: d.__setitem__("sessions", {}),
            "session node list": lambda d: d["sessions"].__setitem__(0, []),
            "kitty wrong": lambda d: s0(d).__setitem__("kitty", "yes"),
            "kitty missing field": lambda d: s0(d)["kitty"].__delitem__("socket"),
            "orphans list": lambda d: d.__setitem__("orphans", []),
            "orphan worktree null": lambda d: d["orphans"]["worktrees"].__setitem__(0, None),
            "warning message int": lambda d: d.__setitem__("warnings", [{"source": "x", "message": 1}]),
            "version bool": lambda d: d.__setitem__("version", True),
            "version string": lambda d: d.__setitem__("version", "1"),
            "required null": lambda d: s0(d)["session"].__setitem__("name", None),
            "provider int": lambda d: a0(d).__setitem__("provider", 3),
            "provider null": lambda d: a0(d).__setitem__("provider", None),
            "quality enum": lambda d: a0(d).__setitem__("status_quality", "great"),
            "note int": lambda d: a0(d).__setitem__("status_note", 1),
            "kind int": lambda d: a0(d).__setitem__("kind", 1),
            "api_ok string": lambda d: a0(d).__setitem__("api_ok", "true"),
            "port string": lambda d: a0(d).__setitem__("port", "5000"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.bad(mutate)
        with self.assertRaises(SnapshotFormatError):
            from_dict([])                                  # type: ignore[arg-type]

    def test_valid_variants_still_accepted(self):
        d = copy.deepcopy(self.good)
        d["taken_at"] = 1_800_000_000                       # int for a float field
        d["sessions"][0]["kitty"] = None
        d["sessions"][0]["worktrees"][0]["worktree"]["dirty"] = None
        del d["warnings"]                                   # defaulted field may be absent
        del d["version"]
        back = from_dict(d)
        self.assertEqual(back.taken_at, NOW)
        self.assertIsInstance(back.taken_at, float)
        self.assertIsNone(back.sessions[0].kitty)
        self.assertEqual(back.warnings, ())


class AgentStartTimeTest(unittest.TestCase):
    """Agent.start_time (pid-reuse identity) round-trips; caches written before it existed load."""

    def snap_with_start(self, start):
        snap = fixture_snapshot(NOW)
        s0 = snap.sessions[0]
        wt = s0.worktrees[1]
        agent = replace(wt.agents[0], start_time=start)
        wt2 = replace(wt, agents=(agent,) + wt.agents[1:])
        return replace(snap, sessions=(replace(s0, worktrees=(s0.worktrees[0], wt2) + s0.worktrees[2:]),)
                       + snap.sessions[1:])

    def test_round_trip(self):
        for start in (1_791_504_414.211443, None):
            with self.subTest(start=start):
                snap = self.snap_with_start(start)
                d = json.loads(json.dumps(to_dict(snap)))
                self.assertEqual(d["sessions"][0]["worktrees"][1]["agents"][0]["start_time"], start)
                back = from_dict(d)
                self.assertEqual(back, snap)
                self.assertEqual(back.sessions[0].worktrees[1].agents[0].start_time, start)

    def test_old_cache_without_field(self):
        d = json.loads(json.dumps(to_dict(self.snap_with_start(123.5))))
        for a in agent_dicts(d):
            a.pop("start_time", None)
        back = from_dict(d)
        self.assertIsNone(back.sessions[0].worktrees[1].agents[0].start_time)
        self.assertEqual(back, self.snap_with_start(None))

    def test_strict(self):
        for bad in ("123", True, float("inf")):
            d = to_dict(self.snap_with_start(1.0))
            d["sessions"][0]["worktrees"][1]["agents"][0]["start_time"] = bad
            with self.subTest(bad=bad), self.assertRaises(SnapshotFormatError):
                from_dict(d)
        d = to_dict(self.snap_with_start(None))
        d["sessions"][0]["worktrees"][1]["agents"][0]["start_time"] = 1700000000   # int accepted as float
        self.assertIsInstance(from_dict(d).sessions[0].worktrees[1].agents[0].start_time, float)


class AgentModelTest(unittest.TestCase):
    def test_meta_copied_and_hash_ignores_it(self):
        src = {"port": 1, "api_ok": True}
        a = Agent(pid=1, cwd=None, session_name=None, pane_id=None, meta=src)
        src["port"] = 2
        self.assertEqual(a.meta, {"port": 1, "api_ok": True})
        self.assertIsInstance(a.meta, dict)
        self.assertIsNot(a.meta, src)
        b = replace(a, meta={"port": 9})
        self.assertNotEqual(a, b)                          # meta takes part in ==
        self.assertEqual(hash(a), hash(b))                 # ... but not in hash()
        self.assertEqual(len({a, b, replace(a)}), 2)
        self.assertEqual(Agent(pid=1, cwd=None, session_name=None, pane_id=None).meta, {})

    def test_defaults(self):
        a = Agent(pid=1, cwd=None, session_name=None, pane_id=None)
        self.assertEqual((a.provider, a.session_id, a.status_source, a.status_quality, a.status_note),
                         ("opencode", None, None, "unavailable", None))
        for gone in ("kind", "oc_session_id", "cc_session_id", "port", "api_ok"):
            self.assertFalse(hasattr(a, gone), gone)


class AgentCompatTest(unittest.TestCase):
    """Legacy alias encode / decode around the provider-neutral Agent model."""

    def enc(self, agent: Agent) -> dict:
        return json.loads(json.dumps(to_dict(with_agents((agent,)))))["orphans"]["agents"][0]

    def dec(self, rec: dict) -> Agent:
        return from_dict({"version": 1, "taken_at": NOW, "orphans": {"agents": [rec]}}).orphans.agents[0]

    def test_encode_aliases(self):
        snap = fixture_snapshot(NOW)
        oc, cc, other = snap.sessions[0].worktrees[1].agents
        self.assertEqual({k: self.enc(oc)[k] for k in ("kind", "oc_session_id", "cc_session_id", "port", "api_ok")},
                         {"kind": "opencode", "oc_session_id": "ses_a1", "cc_session_id": None, "port": 5000,
                          "api_ok": True})
        self.assertEqual({k: self.enc(cc)[k] for k in ("kind", "oc_session_id", "cc_session_id", "port", "api_ok")},
                         {"kind": "claude", "oc_session_id": None, "cc_session_id": CLAUDE_SID, "port": None,
                          "api_ok": False})
        e = self.enc(other)
        self.assertEqual({k: e[k] for k in ("kind", "oc_session_id", "cc_session_id", "port", "api_ok")},
                         {"kind": "codex", "oc_session_id": None, "cc_session_id": None, "port": None,
                          "api_ok": False})
        self.assertEqual((e["provider"], e["session_id"], e["meta"]),
                         ("codex", "cx-1", {"model": "o4", "turns": 3, "ratio": 0.5, "sandbox": False,
                                            "extra": None}))
        # opencode meta without port / api_ok still yields well-typed aliases
        e = self.enc(Agent(pid=1, cwd=None, session_name=None, pane_id=None, meta={"port": True}))
        self.assertEqual((e["port"], e["api_ok"]), (None, False))
        self.assertEqual(self.dec(e).meta, {"port": True})

    def old_opencode(self) -> dict:
        """An opencode record as written before claude support (no kind / cc_session_id /
        status_source / provider / meta)."""
        return {"pid": 4101, "cwd": SSO, "port": 5000, "session_name": "web-app", "pane_id": 2,
                "status": "busy", "status_since_s": 120, "title": "Refactor", "oc_session_id": "ses_a1",
                "last_message": "hi", "context_tokens": 10, "context_limit": 100, "api_ok": True,
                "start_time": 12.5}

    def test_old_opencode_cache_without_kind(self):
        a = self.dec(self.old_opencode())
        self.assertEqual(a, Agent(pid=4101, cwd=SSO, session_name="web-app", pane_id=2, status="busy",
                                  status_since_s=120, title="Refactor", last_message="hi", context_tokens=10,
                                  context_limit=100, start_time=12.5, provider="opencode",
                                  session_id="ses_a1", status_quality="exact",
                                  meta={"port": 5000, "api_ok": True}))
        rec = self.old_opencode()
        rec.update(api_ok=False, port=None)
        a = self.dec(rec)
        self.assertEqual((a.meta, a.status_quality, a.status_note),
                         ({"port": None, "api_ok": False}, "unavailable", "Live status unavailable"))
        rec = self.old_opencode()
        del rec["api_ok"]                                   # defaulted in the old model
        self.assertEqual(self.dec(rec).meta, {"port": 5000, "api_ok": False})

    def test_claude_cache_with_kind_and_cc_session_id(self):
        rec = {"pid": 4302, "cwd": SSO, "port": None, "session_name": "engine", "pane_id": 3,
               "status": "busy", "status_since_s": 45, "title": "Vectorise", "oc_session_id": None,
               "last_message": None, "context_tokens": 61000, "context_limit": 200000, "api_ok": False,
               "start_time": None, "kind": "claude", "cc_session_id": CLAUDE_SID, "status_source": "hooks"}
        a = self.dec(rec)
        self.assertEqual(a, Agent(pid=4302, cwd=SSO, session_name="engine", pane_id=3, status="busy",
                                  status_since_s=45, title="Vectorise", context_tokens=61000,
                                  context_limit=200000, provider="claude", session_id=CLAUDE_SID,
                                  status_source="hooks", status_quality="exact"))
        for src, quality, note in (("registry", "approximate", "Status is approximate"),
                                   ("transcript", "approximate", "Status is approximate"),
                                   (None, "unavailable", None)):
            with self.subTest(src=src):
                a = self.dec({**rec, "status_source": src})
                self.assertEqual((a.status_quality, a.status_note), (quality, note))
        # the legacy record re-encodes with canonical keys and the same aliases
        e = self.enc(a)
        self.assertEqual((e["kind"], e["cc_session_id"], e["oc_session_id"], e["port"], e["api_ok"]),
                         ("claude", CLAUDE_SID, None, None, False))

    def test_canonical_record_with_meta(self):
        rec = {"pid": 7, "cwd": None, "session_name": None, "pane_id": None, "provider": "opencode",
               "session_id": "ses_x", "status_quality": "exact", "meta": {"port": 4096, "api_ok": True}}
        a = self.dec(rec)
        self.assertEqual((a.provider, a.session_id, a.meta, a.status_quality),
                         ("opencode", "ses_x", {"port": 4096, "api_ok": True}, "exact"))
        # canonical, no status_quality: the field default, never inferred
        a = self.dec({k: v for k, v in rec.items() if k != "status_quality"})
        self.assertEqual(a.status_quality, "unavailable")
        # aliases that agree are fine
        self.assertEqual(self.dec({**rec, "kind": "opencode", "oc_session_id": "ses_x", "cc_session_id": None,
                                   "port": 4096, "api_ok": True}), self.dec(rec))
        # meta values keep their JSON types
        a = self.dec({**rec, "provider": "codex", "meta": {"i": 1, "f": 1.0, "b": True, "s": "x", "n": None}})
        self.assertEqual(a.meta, {"i": 1, "f": 1.0, "b": True, "s": "x", "n": None})
        self.assertEqual([type(v) for v in a.meta.values()], [int, float, bool, str, type(None)])

    def test_conflicting_aliases_rejected(self):
        canon = self.enc(fixture_snapshot(NOW).sessions[0].worktrees[1].agents[0])   # opencode
        claude = self.enc(fixture_snapshot(NOW).sessions[0].worktrees[1].agents[1])
        old = self.old_opencode()
        cases = {
            "kind vs provider": {**canon, "kind": "claude"},
            "oc_session_id vs session_id": {**canon, "oc_session_id": "ses_other"},
            "cc_session_id set for opencode": {**canon, "cc_session_id": "x"},
            "oc_session_id set for claude": {**claude, "oc_session_id": "ses_a1"},
            "port vs meta": {**canon, "port": 1},
            "api_ok vs meta": {**canon, "api_ok": False},
            "port for claude": {**claude, "port": 5000},
            "api_ok for claude": {**claude, "api_ok": True},
            "null session_id with alias": {**canon, "session_id": None},
            "legacy: cc_session_id on old opencode": {**old, "cc_session_id": "x"},
            "legacy: claude kind with port": {**old, "kind": "claude", "oc_session_id": None},
        }
        for name, rec in cases.items():
            with self.subTest(name), self.assertRaises(SnapshotFormatError):
                self.dec(rec)

    def test_unknown_provider_decodes(self):
        rec = {"pid": 9, "cwd": "/w", "session_name": "s", "pane_id": 4, "provider": "aider",
               "session_id": "a-1", "status": "idle", "meta": {"x": 1}}
        a = self.dec(rec)
        self.assertEqual((a.provider, a.session_id, a.meta, a.status), ("aider", "a-1", {"x": 1}, "idle"))
        self.assertEqual(self.dec(self.enc(a)), a)
        # legacy-shaped record of an unknown kind
        a = self.dec({"pid": 9, "cwd": None, "session_name": None, "pane_id": None, "kind": "gemini",
                      "port": None, "api_ok": False, "oc_session_id": None, "cc_session_id": None})
        self.assertEqual((a.provider, a.session_id, a.meta), ("gemini", None, {}))

    def test_invalid_meta_rejected(self):
        base = {"pid": 9, "cwd": None, "session_name": None, "pane_id": None, "provider": "codex"}
        for name, meta in {"list value": {"x": [1]}, "dict value": {"x": {"y": 1}}, "nan": {"x": float("nan")},
                           "inf": {"x": float("inf")}, "-inf": {"x": float("-inf")}, "not an object": [1],
                           "string": "x", "null": None, "non-string key": {1: "x"}}.items():
            with self.subTest(name), self.assertRaises(SnapshotFormatError):
                self.dec({**base, "meta": meta})

    def test_snapshot_level_round_trip_of_legacy_cache(self):
        """A whole cache written by the old model loads and re-encodes canonically."""
        d = json.loads(json.dumps(to_dict(fixture_snapshot(NOW))))
        for a in agent_dicts(d):
            for k in ("provider", "session_id", "meta", "status_quality", "status_note"):
                a.pop(k)
            if a["kind"] == "opencode":
                del a["kind"]                               # pre-claude caches had no kind
        back = from_dict(d)
        want = [(a.provider, a.session_id if a.provider in ("opencode", "claude") else None)
                for a in agents_of(fixture_snapshot(NOW))]   # codex: no legacy session alias
        got = agents_of(back)
        self.assertEqual([(a.provider, a.session_id) for a in got], want)
        self.assertEqual(got[0].meta, {"port": 5000, "api_ok": True})
        self.assertEqual(got[2].meta, {})                   # codex: meta not recoverable from aliases
        self.assertEqual(from_dict(to_dict(back)), back)


class FmtTest(unittest.TestCase):
    def test_age(self):
        self.assertEqual(fmt.age(0), "<1m")
        self.assertEqual(fmt.age(59), "<1m")
        self.assertEqual(fmt.age(120), "2m")
        self.assertEqual(fmt.age(5 * 3600), "5h")
        self.assertEqual(fmt.age(3 * 86400 + 5), "3d")
        self.assertEqual(fmt.age(None), "?")

    def test_paths(self):
        self.assertEqual(fmt.home_short("/Users/me/x", "/Users/me"), "~/x")
        self.assertEqual(fmt.home_short("/Users/me", "/Users/me/"), "~")
        self.assertEqual(fmt.home_short("/Users/meow", "/Users/me"), "/Users/meow")
        self.assertTrue(fmt.is_within("/a/b/c", "/a/b"))
        self.assertFalse(fmt.is_within("/a/bc", "/a/b"))

    def test_tokens_plural(self):
        self.assertEqual(fmt.tokens(999), "999")
        self.assertEqual(fmt.tokens(12_345), "12.3k")
        self.assertEqual(fmt.tokens(200_000), "200k")
        self.assertEqual(fmt.tokens(1_500_000), "1.5M")
        self.assertEqual(fmt.plural(1, "tab"), "1 tab")
        self.assertEqual(fmt.plural(2, "tab"), "2 tabs")


if __name__ == "__main__":
    unittest.main()
