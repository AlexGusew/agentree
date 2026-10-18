import unittest
from dataclasses import replace

from agentree.model.filters import apply_filters
from agentree.model.sample import sample_snapshot
from agentree.model.tree import (
    build_view,
    cycle_worktree_mode,
    first_child_key,
    FooterCounts,
    footer_parts,
    footer_text,
    parent_key,
    render_plain,
    resolve_selection,
    select_move,
    toggle,
    toggle_fold,
)
from agentree.types import (
    WORKTREE_MODES, Agent, Seg, SessionNode, Snapshot, SourceWarning, UiState, Worktree, WorktreeNode, ZSession, ZTab,
)

NOW = 1_800_000_000.0
WEB_WT = "/Users/me/Projects/web-app/.worktrees"
SSO = f"w:web-app:{WEB_WT}/sso-login"
ENGINE_WT = "/Users/me/Projects/engine/.worktrees/simd-hash"


def txt(segs):
    return [s.text for s in segs]


class Base(unittest.TestCase):
    def setUp(self):
        self.snap = sample_snapshot(NOW)

    def view(self, **kw):
        return build_view(self.snap, UiState(**kw), NOW, home="/Users/me")

    def rows(self, **kw):
        return {r.key: r for r in self.view(**kw).rows}

    def labels(self, view, kind=None):
        return ["".join(s.text for s in r.label) for r in view.rows if kind is None or r.kind == kind]


class DefaultViewTest(Base):
    def test_default_structure(self):
        v = self.view()
        self.assertEqual(render_plain(v).splitlines()[0], "▾ ● web-app    3 tabs ◆2 attached")
        self.assertEqual(self.labels(v, "session"), ["web-app", "dotfiles", "engine", "scratch"])
        self.assertEqual(self.labels(v, "group"), ["Needs a home"])
        self.assertEqual([r.kind for r in v.rows if not r.selectable], [])   # no footer, no warnings
        self.assertEqual((v.hidden_worktrees, v.hidden_sessions, v.hidden_by_text), (0, 0, 0))

    def test_session_rows(self):
        r = self.rows()
        web, eng, scr = r["s:web-app"], r["s:engine"], r["s:scratch"]
        self.assertEqual((web.icon.text, web.icon.role), ("●", "green"))
        self.assertTrue(web.label[0].bold)
        self.assertEqual(txt(web.meta), ["3 tabs", "◆2", "attached"])
        self.assertEqual([s.role for s in web.meta], ["dim", "accent", "green"])
        self.assertEqual((eng.icon.text, eng.icon.role), ("○", "dim"))
        self.assertEqual(txt(eng.meta), ["2 tabs", "◆2", "detached 5h"])     # opencode + claude
        self.assertEqual((scr.icon.text, scr.icon.role), ("✕", "red"))
        self.assertEqual(txt(scr.meta), ["exited 3d"])
        self.assertEqual(scr.meta[0].role, "red")
        self.assertIsNone(scr.caret)
        self.assertEqual(txt(r["s:dotfiles"].meta), ["1 tab", "◆1", "attached"])

    def test_ages_advance_with_now(self):
        v = build_view(self.snap, UiState(), NOW + 3600, home="/Users/me")
        r = {x.key: x for x in v.rows}
        self.assertEqual(txt(r["s:engine"].meta)[-1], "detached 6h")
        self.assertEqual(txt(r["a:4101"].meta), ["busy 1h"])

    def test_worktree_rows(self):
        r = self.rows()
        root = r["w:web-app:/Users/me/Projects/web-app"]
        self.assertEqual((root.icon.text, root.icon.role), ("⎇", "dim"))
        self.assertEqual(txt(root.meta), ["root", "tab 1", "clean"])
        sso = r[SSO]
        self.assertEqual(sso.icon.role, "teal")
        self.assertEqual(txt(sso.label), ["feat/sso-login"])
        self.assertEqual(txt(sso.meta), ["tab 2", "±6", "↑3"])
        self.assertEqual([s.role for s in sso.meta], ["dim", "red", "blue"])
        self.assertEqual(txt(r[f"w:web-app:{WEB_WT}/flaky-e2e"].meta), ["tab 3", "±2", "↑1"])
        be = r[f"w:web-app:{WEB_WT}/billing-export"]
        self.assertEqual(txt(be.meta), ["no tab", "clean", "↓4"])
        self.assertEqual(be.meta[0].role, "red")
        self.assertEqual(txt(r[f"w:web-app:{WEB_WT}/session-store"].meta), ["no tab", "±3", "↑1"])
        self.assertEqual(sso.parent, "s:web-app")
        self.assertEqual(sso.session, "web-app")
        self.assertEqual(sso.depth, 1)

    def test_agent_rows(self):
        r = self.rows(show_detached=True, folds=(("s:engine", True),))
        a = r["a:4101"]
        self.assertEqual((a.icon.text, a.icon.role), ("◆", "accent"))
        self.assertEqual(txt(a.meta), ["busy 2m"])
        self.assertEqual(a.parent, SSO)
        self.assertEqual(a.depth, 2)
        self.assertEqual((txt(r["a:4102"].meta), r["a:4102"].meta[0].role), (["idle"], "green"))
        self.assertEqual((txt(r["a:4201"].meta), r["a:4201"].meta[0].role), (["needs input"], "red"))
        self.assertEqual((txt(r["a:4301"].meta), r["a:4301"].meta[0].role), (["done 3h"], "dim"))
        orphan = r["a:4401"]
        self.assertEqual((txt(orphan.meta), orphan.meta[0].role), (["busy · no pane"], "red"))
        self.assertEqual(orphan.parent, "g:orphans")
        self.assertIsNone(orphan.session)

    def test_sample_claude_agent_row_is_a_plain_agent(self):
        r = self.rows(folds=(("s:engine", True), (f"w:engine:{ENGINE_WT}", True)))
        a = r["a:4302"]
        self.assertEqual((a.icon.text, a.icon.role), ("◆", "accent"))           # same glyph, status colour
        self.assertEqual(txt(a.label), ["Vectorise the probe loop"])
        self.assertEqual(txt(a.meta), ["busy <1m"])                             # no provider tag
        self.assertEqual(a.parent, f"w:engine:{ENGINE_WT}")
        self.assertEqual(txt(r["a:4301"].meta), ["done 3h"])
        self.assertEqual(r["a:4301"].icon.text, "◆")
        # the pane hosting the agent is not listed as a pane
        rp = self.rows(show_panes=True, folds=(("s:engine", True), (f"w:engine:{ENGINE_WT}", True)))
        self.assertNotIn("p:engine:terminal_3", rp)
        self.assertIn("p:engine:terminal_1", rp)

    def test_untitled_agent(self):
        sn = self.snap.sessions[0]
        wn = sn.worktrees[1]
        wn2 = replace(wn, agents=(replace(wn.agents[0], title=None, status="idle", pane_id=None),))
        snap = replace(self.snap, sessions=(replace(sn, worktrees=(sn.worktrees[0], wn2)),))
        v = build_view(snap, UiState(), NOW)
        a = [r for r in v.rows if r.kind == "agent"][0]
        self.assertEqual((a.label[0].text, a.label[0].role), ("untitled", "dim"))
        self.assertEqual(txt(a.meta), ["idle · no pane"])

    def test_orphan_group(self):
        r = self.rows()
        g = r["g:orphans"]
        self.assertEqual((g.kind, g.caret, g.depth), ("group", "open", 0))
        ow = r["ow:/Users/me/Projects/api/.worktrees/rate-limit"]
        self.assertEqual(ow.parent, "g:orphans")
        self.assertEqual(txt(ow.meta), ["no tab", "±2", "↑5"])
        self.assertEqual(txt(g.meta), ["1 worktree · 1 agent"])


class ModeTest(Base):
    def wt_labels(self, mode):
        v = self.view(worktree_mode=mode, folds=(("s:engine", True),))
        return self.labels(v, "worktree"), v

    def test_all(self):
        labels, v = self.wt_labels("all")
        self.assertEqual(len(labels), 9)
        self.assertEqual(v.hidden_worktrees, 0)

    def test_with_tab(self):
        labels, v = self.wt_labels("with_tab")
        self.assertEqual(labels, ["main", "feat/sso-login", "fix/flaky-e2e", "main", "main", "perf/simd-hash"])
        self.assertEqual(v.hidden_worktrees, 3)
        self.assertEqual(v.rows[-1].kind, "footer")
        self.assertEqual(self.labels(v)[-1], "3 worktrees hidden by filter · W cycles")
        # the orphan group still shows its agent
        self.assertIn("g:orphans", [r.key for r in v.rows])

    def test_dirty(self):
        labels, v = self.wt_labels("dirty")
        self.assertEqual(labels, ["feat/sso-login", "fix/flaky-e2e", "chore/session-store", "main",
                                  "perf/simd-hash", "agent/rate-limit"])
        self.assertEqual(v.hidden_worktrees, 3)

    def test_hide_reparents_with_branch_tag(self):
        v = self.view(worktree_mode="hide", show_panes=True)
        self.assertEqual(self.labels(v, "worktree"), [])
        self.assertEqual(v.hidden_worktrees, 9)
        r = {x.key: x for x in v.rows}
        a = r["a:4101"]
        self.assertEqual((a.parent, a.depth), ("s:web-app", 1))
        self.assertEqual(txt(a.meta), ["busy 2m", "⎇ feat/sso-login"])
        self.assertEqual(a.meta[1].role, "teal")
        nvim = r["p:web-app:terminal_0"]
        self.assertEqual((nvim.parent, txt(nvim.meta)), ("s:web-app", ["⎇ main"]))
        htop = r["p:web-app:terminal_7"]                        # loose pane: no tag
        self.assertEqual(txt(htop.meta), [])
        self.assertEqual(r["a:4201"].parent, "s:dotfiles")
        self.assertEqual(txt(r["a:4201"].meta), ["needs input", "⎇ main"])
        self.assertEqual(self.labels(v)[-1], "9 worktrees hidden by filter · W cycles")

    def test_cycle(self):
        ui = UiState()
        seen = []
        for _ in range(4):
            ui = cycle_worktree_mode(ui)
            seen.append(ui.worktree_mode)
        self.assertEqual(seen, ["with_tab", "dirty", "hide", "all"])
        self.assertEqual(set(seen), set(WORKTREE_MODES))


class ToggleTest(Base):
    def test_agents_off(self):
        v = self.view(show_agents=False)
        self.assertEqual(self.labels(v, "agent"), [])
        r = {x.key: x for x in v.rows}
        self.assertIsNone(r[SSO].caret)                         # no visible children
        self.assertEqual(txt(r["g:orphans"].meta), ["1 worktree"])
        self.assertEqual(txt(r["s:web-app"].meta), ["3 tabs", "◆2", "attached"])  # counts unaffected

    def test_panes_on(self):
        v = self.view(show_panes=True)
        r = {x.key: x for x in v.rows}
        root = "w:web-app:/Users/me/Projects/web-app"
        self.assertEqual(r[root].caret, "closed")               # has children now but no agents
        v = self.view(show_panes=True, folds=((root, True),))
        r = {x.key: x for x in v.rows}
        nvim = r["p:web-app:terminal_0"]
        self.assertEqual((nvim.icon.text, nvim.icon.role), ("▪", "dim"))
        self.assertEqual(txt(nvim.label), ["nvim", "  src/app.tsx"])
        self.assertEqual(txt(r["p:web-app:terminal_1"].label), ["fish", "  ~/Projects/web-app"])
        self.assertEqual(r["p:web-app:terminal_1"].meta, ())
        self.assertEqual(r["p:web-app:terminal_7"].parent, "s:web-app")
        self.assertEqual(txt(r["p:web-app:terminal_7"].label), ["htop", "  ~"])
        self.assertEqual(r[f"p:web-app:terminal_4"].parent, SSO)
        self.assertNotIn("p:web-app:terminal_2", r)            # agent-hosting pane
        self.assertNotIn("p:web-app:terminal_6", r)            # exited pane
        self.assertFalse(any(k.startswith("p:") and "plugin" in k for k in r))

    def test_detached_off(self):
        v = self.view(show_detached=False)
        self.assertEqual(self.labels(v, "session"), ["web-app", "dotfiles"])
        self.assertEqual(v.hidden_sessions, 2)
        # D hides detached engine and exited scratch: labelled accurately
        self.assertEqual(self.labels(v)[-1], "1 detached + 1 exited sessions hidden")
        f = apply_filters(self.snap, UiState(show_detached=False))
        self.assertEqual((f.hidden_detached, f.hidden_exited), (1, 1))

    def test_exited_session_is_session_only(self):
        r = self.rows(text="scratch")
        self.assertEqual([k for k in r if k != "footer"], ["s:scratch"])
        self.assertIsNone(r["s:scratch"].caret)
        # even a (legacy/cached) snapshot that gives an exited session children renders it bare
        web = self.snap.sessions[0]
        dead = replace(web, session=replace(web.session, state="exited", is_current=False))
        v = build_view(replace(self.snap, sessions=(dead,)), UiState(), NOW)
        rows = {x.key: x for x in v.rows}
        self.assertIsNone(rows["s:web-app"].caret)
        self.assertFalse(any(x.session == "web-app" and x.kind != "session" for x in v.rows))

    def test_toggle_helper(self):
        self.assertFalse(toggle(UiState(), "show_agents").show_agents)
        self.assertTrue(toggle(UiState(), "show_panes").show_panes)


class TextFilterTest(Base):
    def test_session_name_match_keeps_all_worktrees(self):
        v = self.view(text="WEB")
        self.assertEqual(self.labels(v, "session"), ["web-app"])
        self.assertEqual(len(self.labels(v, "worktree")), 5)
        self.assertEqual(v.hidden_by_text, 3)
        self.assertIn("3 sessions hidden by text", self.labels(v)[-1])
        self.assertNotIn("g:orphans", [r.key for r in v.rows])

    def test_session_name_match_respects_mode(self):
        v = self.view(text="web", worktree_mode="with_tab")
        self.assertEqual(len(self.labels(v, "worktree")), 3)
        # web-app matches by name (all its worktrees kept); the orphan rate-limit is hidden by text
        self.assertEqual(self.labels(v)[-1], "2 worktrees hidden by filter · 1 hidden by text · "
                                             "3 sessions hidden by text · W cycles · esc clears")

    def test_branch_match(self):
        v = self.view(text="billing")
        self.assertEqual(self.labels(v, "session"), ["web-app"])
        self.assertEqual(self.labels(v, "worktree"), ["feat/billing-export"])
        # 4 web-app worktrees + the orphan rate-limit are hidden by text; the worktrees of the
        # 3 entirely hidden sessions are not double counted
        f = apply_filters(self.snap, UiState(text="billing"))
        self.assertEqual((f.hidden_text_worktrees, f.hidden_by_text, f.hidden_worktrees), (5, 3, 0))
        self.assertEqual(v.hidden_by_text, 3)
        self.assertEqual(v.rows[-1].kind, "footer")
        self.assertEqual(self.labels(v)[-1],
                         "5 worktrees hidden by text · 3 sessions hidden by text · esc clears")

    def test_text_hidden_worktrees_with_mode(self):
        v = self.view(text="billing", worktree_mode="dirty")    # billing-export is clean
        self.assertEqual(self.labels(v)[-1], "1 worktree hidden by filter · 5 hidden by text · "
                                             "3 sessions hidden by text · W cycles · esc clears")

    def test_orphan_worktree_hidden_by_text(self):
        f = apply_filters(self.snap, UiState(text="web"))
        self.assertEqual((f.hidden_text_worktrees, f.hidden_by_text), (1, 3))
        v = self.view(text="web")
        self.assertNotIn("ow:/Users/me/Projects/api/.worktrees/rate-limit", [r.key for r in v.rows])
        self.assertEqual(self.labels(v)[-1],
                         "1 worktree hidden by text · 3 sessions hidden by text · esc clears")

    def test_branch_match_across_sessions_and_orphans(self):
        v = self.view(text="main")
        self.assertEqual(self.labels(v, "session"), ["web-app", "dotfiles", "engine"])
        self.assertEqual(self.labels(v, "worktree"), ["main", "main", "main"])
        self.assertEqual(v.hidden_by_text, 1)
        # text active -> ancestors expanded by default, incl. detached engine
        self.assertEqual({r.caret for r in v.rows if r.kind == "session"}, {"open"})

    def test_agent_title_match(self):
        v = self.view(text="refresh tests")
        self.assertEqual(self.labels(v, "session"), ["web-app"])
        self.assertEqual(self.labels(v, "worktree"), ["feat/sso-login"])
        self.assertEqual(self.labels(v, "agent"), ["Write refresh tests"])

    def test_orphan_agent_title_match(self):
        v = self.view(text="rate limiter")
        self.assertEqual(self.labels(v, "session"), [])
        self.assertEqual(self.labels(v, "group"), ["Needs a home"])
        self.assertEqual(self.labels(v, "agent"), ["Add rate limiter"])
        self.assertEqual(self.labels(v, "worktree"), [])
        self.assertEqual(v.hidden_by_text, 4)

    def test_no_match(self):
        v = self.view(text="zzz")
        self.assertEqual([r.kind for r in v.rows], ["footer"])
        # only the orphan worktree counts separately; session worktrees are in the session count
        self.assertEqual(self.labels(v), ["1 worktree hidden by text · 4 sessions hidden by text · esc clears"])

    def test_explicit_fold_overrides_text_expansion(self):
        v = self.view(text="main", folds=(("s:engine", False),))
        self.assertEqual({r.key: r for r in v.rows}["s:engine"].caret, "closed")


class FoldAndSelectTest(Base):
    def test_fold_defaults(self):
        r = self.rows()
        self.assertEqual(r["s:web-app"].caret, "open")
        self.assertEqual(r["s:dotfiles"].caret, "open")
        self.assertEqual(r["s:engine"].caret, "closed")
        self.assertEqual(r[SSO].caret, "open")
        self.assertIsNone(r[f"w:web-app:{WEB_WT}/flaky-e2e"].caret)
        self.assertNotIn("w:engine:/Users/me/Projects/engine", r)

    def test_fold_overrides(self):
        ui = UiState()
        v = build_view(self.snap, ui, NOW)
        ui = toggle_fold(ui, v, "s:web-app")
        self.assertEqual(ui.folds, (("s:web-app", False),))
        v = build_view(self.snap, ui, NOW)
        keys = [r.key for r in v.rows]
        self.assertNotIn(SSO, keys)
        self.assertEqual({r.key: r for r in v.rows}["s:web-app"].caret, "closed")
        ui = toggle_fold(ui, v, "s:web-app")
        self.assertEqual(ui.folds, (("s:web-app", True),))
        ui = toggle_fold(ui, v, "s:engine")
        v = build_view(self.snap, ui, NOW)
        self.assertIn("w:engine:/Users/me/Projects/engine", [r.key for r in v.rows])
        # no-op on leaf rows
        self.assertEqual(toggle_fold(ui, v, "a:4101"), ui)
        self.assertEqual(toggle_fold(ui, v, "nope"), ui)

    def test_select_move_skips_unselectable(self):
        snap = replace(self.snap, warnings=(SourceWarning("kitty", "no socket"),))
        v = build_view(snap, UiState(show_detached=False), NOW)
        self.assertEqual(v.rows[0].kind, "warning")
        self.assertFalse(v.rows[0].selectable)
        self.assertEqual(v.rows[0].label[0].text, "kitty: no socket")
        self.assertEqual(v.rows[-1].kind, "footer")
        sel = [r.key for r in v.rows if r.selectable]
        self.assertEqual(select_move(v, None, 1), sel[0])
        self.assertEqual(select_move(v, None, -1), sel[-1])
        self.assertEqual(select_move(v, sel[0], -1), sel[0])
        self.assertEqual(select_move(v, sel[0], 1), sel[1])
        self.assertEqual(select_move(v, sel[-1], 5), sel[-1])
        self.assertEqual(select_move(v, sel[1], -10), sel[0])
        self.assertEqual(resolve_selection(v, "warn:0"), sel[0])
        self.assertEqual(resolve_selection(v, sel[2]), sel[2])

    def test_parent_and_child(self):
        v = self.view()
        self.assertEqual(parent_key(v, "a:4101"), SSO)
        self.assertEqual(parent_key(v, SSO), "s:web-app")
        self.assertIsNone(parent_key(v, "s:web-app"))
        self.assertEqual(first_child_key(v, SSO), "a:4101")
        self.assertEqual(first_child_key(v, "s:web-app"), "w:web-app:/Users/me/Projects/web-app")
        self.assertIsNone(first_child_key(v, "s:engine"))       # collapsed
        self.assertIsNone(first_child_key(v, "a:4101"))


class FooterTest(unittest.TestCase):
    def test_pluralisation(self):
        self.assertEqual(footer_text(0, 0, 0), "")
        self.assertEqual(footer_text(1, 1, 1),
                         "1 worktree hidden by filter · 1 session hidden by text · 1 detached session hidden"
                         " · W cycles · esc clears")
        self.assertEqual(footer_text(3, 2, 0), "3 worktrees hidden by filter · 2 detached sessions hidden · W cycles")

    def test_parts(self):
        self.assertEqual(footer_parts(FooterCounts()), [])
        self.assertEqual(footer_parts(FooterCounts(hidden_worktrees=3, hidden_text_worktrees=2, hidden_detached=1)),
                         ["3 worktrees hidden by filter", "2 hidden by text", "1 detached session hidden",
                          "W cycles", "esc clears"])
        self.assertEqual(footer_parts(FooterCounts(hidden_text_worktrees=1)),
                         ["1 worktree hidden by text", "esc clears"])
        self.assertEqual(footer_parts(FooterCounts(hidden_exited=2)), ["2 exited sessions hidden"])
        self.assertEqual(footer_parts(FooterCounts(hidden_exited=1)), ["1 exited session hidden"])
        self.assertEqual(footer_parts(FooterCounts(hidden_detached=1, hidden_exited=2)),
                         ["1 detached + 2 exited sessions hidden"])
        # no W hint without worktree-mode hiding; no esc hint without text hiding
        self.assertNotIn("W cycles", footer_parts(FooterCounts(hidden_by_text=1)))
        self.assertNotIn("esc clears", footer_parts(FooterCounts(hidden_worktrees=1)))


# --------------------------------------------------------------------------- providers look the same

PROV_WT = "/Users/me/Projects/web-app/.worktrees/agents"
PROV_KEY = f"w:web-app:{PROV_WT}"


def provider_agent(pid, provider, **kw):
    """One agent with the same user-facing facts whatever its provider."""
    base = dict(pid=pid, cwd=PROV_WT, session_name="web-app", pane_id=pid % 100, status="busy",
                status_since_s=120, title="Fix the login form", last_message="Done with the first pass.",
                context_tokens=10_000, context_limit=200_000, provider=provider)
    base.update(kw)
    return Agent(**base)


def provider_snapshot(*agents):
    """A tiny linked snapshot: one attached session, one worktree, the given agents in it."""
    agents = agents or (
        provider_agent(9101, "opencode", session_id="ses_1", status_quality="exact",
                       meta={"port": 5000, "api_ok": True}),
        provider_agent(9102, "claude", session_id="0b5e2c4e-7d1a", status_source="hooks", status_quality="exact"),
        provider_agent(9103, "codex", session_id="cx-1", status_quality="exact"),
    )
    tab = ZTab(tab_id=1, position=0, name="agents", active=True)
    wt = Worktree(path=PROV_WT, repo_root="/Users/me/Projects/web-app", branch="feat/agents", head="abc1234",
                  is_root=False, dirty=0)
    sess = ZSession(name="web-app", state="attached", created_age_s=3600, socket_dir="/tmp/z", clients=1,
                    tabs=(tab,))
    node = SessionNode(session=sess, worktrees=(WorktreeNode(worktree=wt, tabs=(tab,), agents=tuple(agents)),))
    return Snapshot(taken_at=NOW, sessions=(node,))


class ProviderNeutralRowsTest(unittest.TestCase):
    def test_every_provider_renders_the_same_row(self):
        snap = provider_snapshot()
        rows = {r.key: r for r in build_view(snap, UiState(), NOW).rows}
        agent_rows = [rows[f"a:{p}"] for p in (9101, 9102, 9103)]
        for r in agent_rows:
            self.assertEqual((r.icon.text, r.icon.role), ("◆", "accent"))
            self.assertEqual(txt(r.label), ["Fix the login form"])
            self.assertEqual(txt(r.meta), ["busy 2m"])                 # no provider tag
            self.assertEqual(r.parent, PROV_KEY)
        self.assertEqual(len({(r.icon, r.label, r.meta) for r in agent_rows}), 1)
        self.assertEqual(txt(rows["s:web-app"].meta), ["1 tab", "◆3", "attached"])

    def test_glyph_colour_follows_status_only(self):
        for status, role in (("busy", "accent"), ("idle", "green"), ("needs_input", "red"), ("done", "dim"),
                             ("unknown", "dim")):
            snap = provider_snapshot(provider_agent(9101, "opencode", status=status),
                                     provider_agent(9102, "claude", status=status))
            rows = {r.key: r for r in build_view(snap, UiState(), NOW).rows}
            self.assertEqual(rows["a:9101"].icon, Seg("◆", role))
            self.assertEqual(rows["a:9102"].icon, Seg("◆", role))
            self.assertEqual(rows["a:9101"].meta, rows["a:9102"].meta)

    def test_no_provider_names_in_the_tree(self):
        text = render_plain(build_view(provider_snapshot(), UiState(), NOW))
        for word in ("claude", "opencode", "codex", "✻"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
