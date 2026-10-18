import unittest

from agentree.model.detail import detail_for
from agentree.model.sample import sample_snapshot
from agentree.model.tree import build_view
from agentree.types import Row, SourceWarning, UiState

from tests.test_model_tree import PROV_KEY, provider_agent, provider_snapshot

NOW = 1_800_000_000.0
WEB_WT = "/Users/me/Projects/web-app/.worktrees"


AGENT_FIELDS = ["status", "worktree", "session", "context usage", "pid", "last message"]


def fields(d):
    return {f.label: "".join(s.text for s in f.value) for f in d.fields}


def actions(d):
    return [(a.key, a.label, a.enabled) for a in d.actions]


class DetailTest(unittest.TestCase):
    def setUp(self):
        self.snap = sample_snapshot(NOW)
        ui = UiState(show_panes=True, folds=(("s:engine", True), ("w:web-app:/Users/me/Projects/web-app", True)))
        self.rows = {r.key: r for r in build_view(self.snap, ui, NOW, home="/Users/me").rows}

    def detail(self, key):
        return detail_for(self.rows[key], self.snap, NOW, home="/Users/me")

    def test_session(self):
        d = self.detail("s:web-app")
        self.assertEqual(d.kind, "SESSION")
        self.assertEqual(d.title[0].text, "web-app")
        f = fields(d)
        self.assertEqual(f["state"], "attached · 1 client")
        self.assertEqual(f["tabs"], "3")
        self.assertEqual(f["worktrees"], "5")
        self.assertEqual(f["agents"], "2")
        self.assertEqual(f["layout"], "web-app · sso-login · flaky-e2e")
        self.assertTrue([x for x in d.fields if x.label == "layout"][0].wrap)
        self.assertEqual(actions(d), [("enter", "jump", True), ("r", "rename", True), ("x", "kill", True)])
        f = fields(self.detail("s:scratch"))
        self.assertEqual(f["state"], "exited 3d · resurrectable")
        self.assertEqual(f["layout"], "—")

    def test_worktree(self):
        d = self.detail(f"w:web-app:{WEB_WT}/sso-login")
        self.assertEqual(d.kind, "WORKTREE")
        f = fields(d)
        self.assertEqual(f["path"], "~/Projects/web-app/.worktrees/sso-login")
        self.assertEqual(f["branch"], "feat/sso-login")
        self.assertEqual(f["tab"], "web-app › 2")
        self.assertEqual(f["changes"], "6 files")
        self.assertEqual(f["vs main"], "↑3  origin/main")
        self.assertEqual(f["agents"], "2")
        self.assertEqual(f["last commit"], "Extract token refresh into hook · 25m")
        self.assertEqual(actions(d), [("enter", "jump", True), ("o", "agent here", True),
                                      ("t", "open tab", False), ("g", "lazygit", True), ("x", "rm…", True)])

    def test_worktree_no_tab_and_clean(self):
        d = self.detail(f"w:web-app:{WEB_WT}/billing-export")
        f = fields(d)
        self.assertEqual(f["tab"], "no tab")
        self.assertEqual(f["changes"], "clean")
        self.assertEqual(f["vs main"], "↓4  origin/main")
        self.assertEqual(f["last commit"], "Add CSV export endpoint · 2d")
        self.assertIn(("t", "open tab", True), actions(d))

    def test_root_and_orphan_worktree(self):
        d = self.detail("w:web-app:/Users/me/Projects/web-app")
        self.assertEqual(fields(d)["vs main"], "up to date  origin/main")
        self.assertIn(("x", "rm…", False), actions(d))
        self.assertEqual(fields(d)["flags"], "root checkout")
        d = self.detail("ow:/Users/me/Projects/api/.worktrees/rate-limit")
        self.assertEqual(d.kind, "WORKTREE")
        self.assertEqual(fields(d)["tab"], "no tab")
        self.assertIn(("enter", "jump", False), actions(d))

    def test_agent(self):
        d = self.detail("a:4101")
        self.assertEqual(d.kind, "AGENT")
        self.assertEqual(d.title[0].text, "Refactor token refresh")
        f = fields(d)
        self.assertEqual(f["status"], "busy 2m")
        self.assertEqual(f["worktree"], "feat/sso-login")
        self.assertEqual(f["session"], self.rows["a:4101"].ref.session_id or "unknown")
        self.assertNotIn("server port", f)
        self.assertEqual(f["context usage"], "12.3k / 200k (6%)")
        self.assertTrue(f["last message"].startswith("Moved the refresh logic"))
        self.assertTrue([x for x in d.fields if x.label == "last message"][0].wrap)
        self.assertEqual(actions(d), [("enter", "jump", True), ("x", "stop", True)])

    def test_orphan_agent(self):
        d = self.detail("a:4401")
        f = fields(d)
        self.assertEqual(f["status"], "busy · no pane")
        self.assertEqual(f["worktree"], "~/Projects/api/.worktrees/rate-limit")
        self.assertEqual(f["context usage"], "unknown")
        self.assertEqual(f["last message"], "—")
        self.assertIn(("enter", "jump", False), actions(d))

    def test_sample_claude_agent_is_a_plain_agent(self):
        d = self.detail("a:4302")
        self.assertEqual(d.kind, "AGENT")
        self.assertEqual(d.title[0].text, "Vectorise the probe loop")
        self.assertEqual([x.label for x in d.fields], AGENT_FIELDS)
        f = fields(d)
        self.assertEqual(f["session"], self.rows["a:4302"].ref.session_id or "unknown")
        self.assertEqual(f["worktree"], "perf/simd-hash")
        self.assertEqual(f["context usage"], "61k / 200k (30%)")
        self.assertEqual(actions(d), [("enter", "jump", True), ("x", "stop", True)])

    def test_pane(self):
        d = self.detail("p:web-app:terminal_0")
        self.assertEqual(d.kind, "PANE")
        f = fields(d)
        self.assertEqual(f["command"], "nvim src/app.tsx")
        self.assertEqual(f["worktree"], "main")
        self.assertEqual(f["session"], "web-app")
        self.assertEqual(actions(d), [("enter", "focus", True)])
        self.assertEqual(fields(self.detail("p:web-app:terminal_7"))["worktree"], "none")

    def test_group_and_misc(self):
        d = self.detail("g:orphans")
        self.assertEqual(d.kind, "NEEDS A HOME")
        self.assertEqual(fields(d)["worktrees"], "1")
        self.assertEqual(fields(d)["agents"], "1")
        w = SourceWarning("git", "timeout")
        row = Row(key="warn:0", kind="warning", depth=0, caret=None, icon=None, label=(), meta=(), ref=w,
                  selectable=False)
        self.assertEqual(detail_for(row, self.snap, NOW).kind, "WARNING")

    def test_elapsed(self):
        d = detail_for(self.rows["a:4101"], self.snap, NOW + 600)
        self.assertEqual(fields(d)["status"], "busy 12m")


class ProviderNeutralDetailTest(unittest.TestCase):
    """Every agent gets the same panel, whatever provider found it."""

    def detail(self, *agents, pid=9101):
        snap = provider_snapshot(*agents)
        rows = {r.key: r for r in build_view(snap, UiState(), NOW).rows}
        return detail_for(rows[f"a:{pid}"], snap, NOW, home="/Users/me")

    def test_same_fields_for_every_provider(self):
        details = [self.detail(pid=p) for p in (9101, 9102, 9103)]
        for d in details:
            self.assertEqual(d.kind, "AGENT")
            self.assertEqual([x.label for x in d.fields], AGENT_FIELDS)
            self.assertEqual(actions(d), [("enter", "jump", True), ("x", "stop", True)])
            f = fields(d)
            self.assertEqual((f["status"], f["worktree"], f["context usage"], f["last message"]),
                             ("busy 2m", "feat/agents", "10k / 200k (5%)", "Done with the first pass."))
        self.assertEqual([fields(d)["session"] for d in details], ["ses_1", "0b5e2c4e-7d1a", "cx-1"])
        for d in details:
            text = " ".join([x.label for x in d.fields] + [fields(d)[x.label] for x in d.fields])
            for word in ("server port", "5000", "Claude Code", "claude", "opencode", "codex", "status source",
                         "hooks", "unreachable"):
                self.assertNotIn(word, text)

    def test_unknown_session(self):
        d = self.detail(provider_agent(9101, "claude", session_id=None))
        self.assertEqual(fields(d)["session"], "unknown")

    def test_note_only_when_status_is_not_exact(self):
        cases = (
            ("approximate", "Status is approximate", "Status is approximate"),
            ("unavailable", "Live status unavailable", "Live status unavailable"),
            ("exact", "Status is approximate", None),          # exact: never a note
            ("approximate", None, None),                       # nothing to say
            ("unavailable", None, None),
        )
        for provider in ("opencode", "claude", "codex"):
            for quality, note, want in cases:
                d = self.detail(provider_agent(9101, provider, status_quality=quality, status_note=note))
                labels = [x.label for x in d.fields]
                if want is None:
                    self.assertEqual(labels, AGENT_FIELDS, (provider, quality, note))
                else:
                    self.assertEqual(labels, AGENT_FIELDS + ["note"], (provider, quality, note))
                    nf = d.fields[-1]
                    self.assertEqual(fields(d)["note"], want)
                    self.assertEqual([s.role for s in nf.value], ["dim"])      # subtle, not a warning

    def test_worktree_offers_agent_here_once(self):
        snap = provider_snapshot()
        rows = {r.key: r for r in build_view(snap, UiState(), NOW).rows}
        d = detail_for(rows[PROV_KEY], snap, NOW)
        keys = [a.key for a in d.actions]
        self.assertIn(("o", "agent here", True), actions(d))
        self.assertNotIn("c", keys)


if __name__ == "__main__":
    unittest.main()
