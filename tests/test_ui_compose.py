"""Frame composition on the sample snapshot (pure: no terminal)."""
import unittest
from dataclasses import replace

from agentree.model.sample import sample_snapshot
from agentree.types import Row, Seg, SourceWarning, UiState, View
from agentree.ui.compose import compose, make_state, plain, tree_width
from agentree.ui.theme import Theme

from tests.test_model_tree import provider_snapshot

NOW = 1_800_000_000.0
HOME = "/Users/me"
SSO = "w:web-app:/Users/me/Projects/web-app/.worktrees/sso-login"


def frame(ui=UiState(), size=(120, 32), snap=None, **kw):
    st = make_state(snap or sample_snapshot(NOW), ui, now=NOW, home=HOME)
    for k, v in kw.items():
        setattr(st, k, v)
    cv = compose(st, size)
    return st, cv, plain(cv)


def chip_active(cv, label):
    """True if the chip `[label]` on the filter bar is drawn with the active (accent) style."""
    line = "".join(cv.chars[1])
    i = line.find("[" + label + "]")
    assert i >= 0, (label, line)
    return cv.styles[1][i + 1][0] == Theme(truecolor=True).c("accent")


class Layout(unittest.TestCase):
    def test_120x32_default(self):
        _, cv, lines = frame()
        self.assertEqual(len(lines), 32)
        self.assertTrue(all(len("".join(r)) <= 120 for r in cv.chars))
        self.assertTrue(lines[0].startswith(" ▍agentree  sessions · worktrees · agents"))
        self.assertTrue(lines[0].endswith("[/ filter by name, branch, agent ]"))
        self.assertTrue(lines[1].startswith(" ⎇ worktrees [all][with tab][dirty][hide] │ "
                                            "[◆ agents][▪ panes][○ detached]"))
        self.assertIn("4 sessions · 9 worktrees · 6 agents", lines[1])
        self.assertIn("┬", lines[2])
        self.assertIn("┴", lines[30])
        self.assertTrue(lines[31].startswith(" ↵ attach  ␣ fold  W worktree filter  A agents  P panes  D detached"))
        self.assertTrue(lines[31].endswith("esc close"))

    def test_chip_states(self):
        _, cv, _ = frame()
        self.assertTrue(chip_active(cv, "all"))
        self.assertFalse(chip_active(cv, "hide"))
        self.assertTrue(chip_active(cv, "◆ agents"))
        self.assertFalse(chip_active(cv, "▪ panes"))
        self.assertTrue(chip_active(cv, "○ detached"))
        _, cv, _ = frame(UiState(worktree_mode="dirty", show_agents=False, show_panes=True))
        self.assertTrue(chip_active(cv, "dirty"))
        self.assertFalse(chip_active(cv, "all"))
        self.assertFalse(chip_active(cv, "◆ agents"))
        self.assertTrue(chip_active(cv, "▪ panes"))

    def test_tree_rows_and_meta(self):
        _, _, lines = frame()
        body = lines[3:30]
        tw = tree_width(120)
        web = next(l for l in body if "● web-app" in l)
        self.assertTrue(web.startswith("▍▾ ● web-app"))           # first row selected by default
        self.assertTrue(web[:tw].rstrip().endswith("3 tabs ◆2 attached"))
        sso = next(l for l in body if "feat/sso-login" in l)
        self.assertTrue(sso.startswith("   ▾ ⎇ feat/sso-login"))   # depth 1, open caret
        self.assertTrue(sso[:tw].rstrip().endswith("tab 2 ±6 ↑3"))
        agent = next(l for l in body if "Refactor token refresh" in l)
        self.assertTrue(agent.startswith("       ◆ Refactor token refresh"))
        self.assertTrue(agent[:tw].rstrip().endswith("busy 2m"))
        self.assertTrue(any(l.startswith(" ▸ ○ engine") for l in body))
        self.assertTrue(any(l.startswith("   ✕ scratch") for l in body))
        self.assertTrue(any("⌂ Needs a home" in l for l in body))
        # all meta right-aligned to the same column
        ends = {len(l[:tw].rstrip()) for l in body if l[:tw].strip()}
        self.assertEqual(ends, {tw - 1})          # one cell of padding before the divider

    def test_detail_session(self):
        _, _, lines = frame()
        tw = tree_width(120)
        right = [l[tw + 1:] for l in lines[3:30]]
        self.assertTrue(any(r.strip().startswith("SESSION ─") for r in right))
        self.assertTrue(any(r.strip() == "web-app" for r in right))
        state = next(r for r in right if "state" in r)
        self.assertIn("attached · 1 client", state)
        # values line up in one column
        cols = {r.index(v) for r, v in ((next(r for r in right if f"  {k} " in r), v)
                                        for k, v in (("tabs", "3"), ("worktrees", "5"), ("agents", "2")))}
        self.assertEqual(len(cols), 1)
        self.assertTrue(any(r.strip() == "[↵ jump][r rename][x kill]" for r in right))

    def test_detail_worktree_and_agent(self):
        _, _, lines = frame(UiState(selected=SSO))
        text = "\n".join(lines)
        for s in ("WORKTREE", "path", "~/Projects/web-app/.worktrees/", "sso-login", "branch", "changes",
                  "6 files", "vs main", "↑3", "last commit", "[↵ jump][o agent here][t open tab]", "[g lazygit][x rm…]"):
            self.assertIn(s, text)
        self.assertTrue(lines[4].startswith("   ▾ ⎇ feat/sso-login") or "▍  ▾ ⎇ feat/sso-login" in text)
        _, _, lines = frame(UiState(selected="a:4101"), size=(90, 24))
        text = "\n".join(lines)
        self.assertIn("AGENT", text)
        self.assertIn("12.3k / 200k (6%)", text)
        # long message wraps onto several lines in the value column
        wrapped = [l for l in lines if "logic into" in l or "useTokenRefresh" in l]
        self.assertTrue(wrapped)

    def test_sample_claude_agent_row_and_detail(self):
        ui = UiState(selected="a:4302", folds=(("s:engine", True),))
        _, _, lines = frame(ui)
        tw = tree_width(120)
        row = next(l for l in lines[3:30] if "Vectorise the probe loop" in l[:tw])
        self.assertTrue(row.startswith("▍      ◆ Vectorise the probe loop"), row)   # selected, depth 2
        self.assertTrue(row[:tw].rstrip().endswith("busy <1m"), row)                # no provider tag
        right = "\n".join(l[tw + 1:] for l in lines[3:30])
        for needle in ("AGENT", "session", "61k / 200k (30%)", "[↵ jump][x stop]"):
            self.assertIn(needle, right)
        for word in ("Claude Code", "status source", "via hooks", "server port", "unreachable"):
            self.assertNotIn(word, right)

    def test_every_provider_draws_the_same(self):
        snap = provider_snapshot()
        tw = tree_width(120)
        tree_rows, panels = [], []
        for pid in (9101, 9102, 9103):
            _, _, lines = frame(UiState(selected=f"a:{pid}"), snap=snap)
            row = next(l for l in lines[3:30] if l.startswith("▍"))
            tree_rows.append(row[:tw])
            right = [l[tw + 1:].rstrip() for l in lines[3:30]]
            # session ids and pids differ, nothing else
            panels.append([r for r in right if not r.strip().startswith(("session", "pid"))])
            self.assertTrue(any(r.strip().startswith("session") for r in right))
        self.assertTrue(tree_rows[0].startswith("▍      ◆ Fix the login form"), tree_rows[0])
        self.assertTrue(tree_rows[0].rstrip().endswith("busy 2m"), tree_rows[0])
        self.assertEqual(tree_rows[0], tree_rows[1])
        self.assertEqual(tree_rows[0], tree_rows[2])
        self.assertEqual(panels[0], panels[1])
        self.assertEqual(panels[0], panels[2])
        text = "\n".join(panels[0])
        for word in ("claude", "Claude", "opencode", "codex", "port"):
            self.assertNotIn(word, text)

    def test_disabled_action_is_struck(self):
        _, cv, lines = frame(UiState(selected=SSO))
        y = next(i for i, l in enumerate(lines) if "[t open tab]" in l)
        x = lines[y].index("open tab")
        self.assertTrue(cv.styles[y][x][2] & 8)   # STRIKE flag: not colour-only

    def test_hide_mode_with_text(self):
        _, _, lines = frame(UiState(worktree_mode="hide", text="sso"))
        self.assertIn("[/ sso", lines[0])
        body = [l for l in lines[3:30] if l[:71].strip()]
        self.assertEqual(len(body), 4)
        self.assertIn("● web-app", body[0])
        self.assertIn("◆ Refactor token refresh", body[1])
        self.assertTrue(body[1][:71].rstrip().endswith("busy 2m ⎇ feat/sso-login"))
        self.assertTrue(body[3].lstrip().startswith("╌╌ 1 worktree hidden by filter"))
        self.assertTrue(lines[31].endswith("esc clear filter"))

    def test_filter_mode_and_indicators(self):
        snap = replace(sample_snapshot(NOW), stale=True)
        _, cv, lines = frame(UiState(text="ss"), snap=snap, mode="filter", refreshing=True)
        self.assertIn("↻ stale", lines[0])
        self.assertIn("[/ ss ", lines[0])
        x = lines[0].index("[/ ss") + 5
        self.assertEqual(cv.styles[0][x][1], Theme(truecolor=True).c("text"))   # block cursor
        self.assertTrue(lines[-1].startswith(" type to filter"))
        self.assertTrue(lines[-1].endswith("esc clear"))

    def test_90x24(self):
        _, _, lines = frame(size=(90, 24))
        self.assertEqual(len(lines), 24)
        self.assertIn("│", lines[5])
        self.assertTrue(lines[0].endswith("[/ filter by name, branch, agent ]"))
        self.assertTrue(lines[-1].endswith("esc close"))
        self.assertIn("SESSION", lines[3])

    def test_narrow_hides_detail(self):
        _, _, lines = frame(size=(89, 24))
        self.assertNotIn("SESSION", "\n".join(lines))
        self.assertNotIn("┬", lines[2])
        self.assertTrue(lines[3].rstrip().endswith("3 tabs ◆2 attached"))

    def test_200x50(self):
        _, _, lines = frame(size=(200, 50))
        self.assertEqual(len(lines), 50)
        self.assertTrue(all(len(l) <= 200 for l in lines))
        self.assertIn("SESSION", "\n".join(lines))
        self.assertTrue(lines[-1].endswith("esc close"))

    def test_tiny(self):
        _, _, lines = frame(size=(30, 6))
        self.assertIn("agentree needs at least", "\n".join(lines))

    def test_status_line(self):
        from agentree.ui.compose import Status
        _, cv, lines = frame(status=Status("jump: not implemented yet", "error"))
        self.assertTrue(lines[-1].startswith(" ✗ jump: not implemented yet"))
        self.assertEqual(cv.styles[-1][3][0], Theme(truecolor=True).c("red"))
        _, _, lines = frame(activity=Status("working… jump", "busy"), tick=3)
        self.assertTrue(lines[-1].startswith(" ⠸ working… jump"))

    def test_warning_rows(self):
        snap = replace(sample_snapshot(NOW), warnings=(SourceWarning("git", "timed out after 1s"),))
        _, _, lines = frame(snap=snap)
        self.assertIn("! git: timed out after 1s", lines[4])


class Truncation(unittest.TestCase):
    def view_frame(self, rows, size=(90, 24), selected=None):
        st = make_state(sample_snapshot(NOW), UiState(), now=NOW, home=HOME)
        st.view = View(rows=tuple(rows))
        st.ui = replace(st.ui, selected=selected or rows[0].key)
        return plain(compose(st, size))

    def test_label_shrinks_before_meta(self):
        r = Row("a:1", "agent", 1, None, Seg("◆", "accent"),
                (Seg("A very long agent title that keeps going and going and going"),),
                (Seg("busy 12m", "accent"), Seg("⎇ feat/some-branch", "teal")))
        lines = self.view_frame([r])
        tw = tree_width(90)
        line = lines[3][:tw]
        self.assertIn("…", line)
        self.assertTrue(line.rstrip().endswith("busy 12m ⎇ feat/some-branch"))
        self.assertIn("◆ A very long", line)

    def test_meta_cut_when_no_room(self):
        r = Row("a:1", "agent", 6, None, Seg("◆", "accent"), (Seg("Title of the agent"),),
                (Seg("x" * 60, "dim"),))
        lines = self.view_frame([r])
        line = lines[3][:tree_width(90)]
        self.assertIn("◆ Title of…", line)          # label keeps a readable minimum (10 cells)
        self.assertTrue(line.rstrip().endswith("…"))

    def test_wide_chars(self):
        r = Row("s:x", "session", 0, None, Seg("●", "green"), (Seg("会議室" * 12),), (Seg("attached", "green"),))
        st = make_state(sample_snapshot(NOW), UiState(), now=NOW, home=HOME)
        st.view = View(rows=(r,))
        st.ui = replace(st.ui, selected="s:x")
        cv = compose(st, (90, 24))
        from agentree.ui.text import width
        line = "".join(cv.chars[3])
        self.assertEqual(width(line), 90)
        self.assertTrue("".join(cv.chars[3][:tree_width(90)]).rstrip().endswith("attached"))
        self.assertIn("…", plain(cv)[3])


class Scrolling(unittest.TestCase):
    def test_selection_stays_visible(self):
        ui = UiState(show_panes=True, folds=(("s:engine", True),))
        st = make_state(sample_snapshot(NOW), ui, now=NOW, home=HOME)
        size = (100, 14)                   # 9 body rows
        keys = [r.key for r in st.view.rows if r.selectable]
        self.assertGreater(len(keys), 12)
        for k in keys:
            st.ui = replace(st.ui, selected=k)
            cv = compose(st, size)
            st.scroll = cv.info["scroll"]
            lines = plain(cv)
            sel = [l for l in lines[3:12] if l.startswith("▍")]
            self.assertEqual(len(sel), 1, (k, lines))
        self.assertGreater(st.scroll, 0)
        # back to the top
        st.ui = replace(st.ui, selected=keys[0])
        cv = compose(st, size)
        self.assertEqual(cv.info["scroll"], 0)

    def test_footer_pinned_when_scrolling(self):
        ui = UiState(show_panes=True, worktree_mode="with_tab", folds=(("s:engine", True),))
        st = make_state(sample_snapshot(NOW), ui, now=NOW, home=HOME)
        lines = plain(compose(st, (100, 12)))
        self.assertIn("╌╌", lines[3 + 7 - 1])
        self.assertIn("┃", "".join(lines[3:10]))         # scrollbar thumb on the divider


class AgentChooser(unittest.TestCase):
    def chooser(self, n=3, default="claude"):
        from agentree.actions.api import AgentChoice
        from agentree.ui.widgets import AgentChooserModal
        base = [AgentChoice("opencode", "opencode", True), AgentChoice("claude", "Claude Code", True),
                AgentChoice("codex", "Codex", True)]
        extra = [AgentChoice(f"p{i}", f"Provider {i}", True) for i in range(max(0, n - 3))]
        return AgentChooserModal(base[:n] + extra, default, payload=None)

    def test_draw_matches_the_other_modals(self):
        m = self.chooser()
        _, cv, lines = frame(snap=provider_snapshot(), modal=m)
        th = Theme(truecolor=True)
        top = next(i for i, l in enumerate(lines) if "╭─ Agent here ─" in l)
        box = lines[top:top + 8]
        x0 = box[0].index("╭")
        self.assertEqual([l[x0] for l in box], ["╭", "│", "│", "│", "│", "│", "│", "╰"])   # 3 choices + 5
        body = [l[x0 + 1:].split("│")[0] for l in box[1:-1]]
        self.assertEqual([b.strip() for b in body],
                         ["", "opencode", "▍ Claude Code", "Codex", "", "↑ ↓ choose   ↵ launch   esc cancel"])
        self.assertTrue(body[1].startswith("  opencode"))               # padding as in the prompt modal
        self.assertEqual(lines[top].index("Agent here"), x0 + 3)
        # the selected row: highlight fill across the box, accent marker, bold label
        y = top + 3
        lx = lines[y].index("Claude Code")
        self.assertEqual(cv.styles[y][x0 + 1], th.st("accent", "selected"))
        self.assertEqual(cv.styles[y][lx], th.st("text", "selected", 1))
        self.assertEqual(cv.styles[y][x0 + 20][1], th.c("selected"))
        self.assertEqual(cv.styles[top + 2][x0 + 3], th.st("text", "modal"))          # others: plain text
        self.assertEqual(cv.styles[top][x0 + 3], th.st("accent", "modal", 1))         # title like the prompt
        hint = lines[top + 6].index("↵")
        self.assertEqual(cv.styles[top + 6][hint][0], th.c("accent"))                 # ↵ is the strong key

    def test_many_providers_scroll(self):
        m = self.chooser(n=40, default="p30")
        _, _, lines = frame(snap=provider_snapshot(), modal=m, size=(90, 24))
        text = "\n".join(lines)
        self.assertIn("Provider 30", text)                              # selection stays in view
        self.assertIn("↑ ↓ choose", text)
        sel = [l for l in lines if "▍ Provider" in l]
        self.assertEqual(len(sel), 1)
        self.assertIn("Provider 30", sel[0])
        for _ in range(50):
            from agentree.ui.term import Key
            m.handle(Key("down", ""))
        self.assertEqual(m.provider, "p36")
        _, _, lines = frame(snap=provider_snapshot(), modal=m, size=(90, 24))
        self.assertIn("▍ Provider 36", "\n".join(lines))

    def test_keys(self):
        from agentree.ui.term import Key
        m = self.chooser()
        self.assertEqual(m.provider, "claude")
        for name, want, res in (("j", "codex", None), ("down", "codex", None), ("k", "claude", None),
                                ("up", "opencode", None), ("up", "opencode", None), ("x", "opencode", None)):
            self.assertEqual(m.handle(Key(name, name if len(name) == 1 else "")), res)
            self.assertEqual(m.provider, want)
        self.assertEqual(m.handle(Key("enter", "")), "ok")
        self.assertEqual(m.handle(Key("esc", "")), "cancel")
        self.assertEqual(m.handle(Key("q", "q")), "cancel")


if __name__ == "__main__":
    unittest.main()
