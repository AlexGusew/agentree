"""`g` lazygit in a floating pane: missing binary, exact floating command, verification, dry-run."""
import unittest
from unittest import mock

try:
    from tests.test_actions_agent import AgentBase, no_wt_tab
    from tests.test_actions_jump import DIR, KT, SOCK, WT, ZJ, R, fake_nav, nav_for, row
except ImportError:  # pragma: no cover
    from test_actions_agent import AgentBase, no_wt_tab  # type: ignore
    from test_actions_jump import DIR, KT, SOCK, WT, ZJ, R, fake_nav, nav_for, row  # type: ignore

from agentree.actions import lazygit as lg
from agentree.actions.real import RealActions
from agentree.config import Config, from_mapping
from agentree.types import WorktreeNode

LG = "/bin/sh"


class LazygitTest(AgentBase):
    def g(self, w, snap, r, cfg=None, **kw):
        return lg.lazygit(nav_for(w, **kw), r, snap, cfg=cfg or Config(lazygit_cmd=LG), state_dir=self.tmp.name)

    def test_missing_binary(self):
        w, snap, node = self.world()
        for cmd in ("/nonexistent/lazygit", "lazygit-not-installed-agentree"):
            res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), cfg=Config(lazygit_cmd=cmd), current="S")
            self.assertFalse(res.ok)
            self.assertEqual(res.message, "lazygit not installed — see https://github.com/jesseduffield/lazygit"
                                          "#installation (or set lazygit_cmd in ~/.config/agentree/config.toml)")
        self.assertEqual(w.calls, [])

    def test_default_command_is_lazygit_on_path(self):
        self.assertEqual(Config().lazygit_cmd, "lazygit")
        self.assertEqual(from_mapping({"lazygit_cmd": "~/bin/lg"}).lazygit_cmd, "~/bin/lg")
        self.assertIn("lazygit_cmd: expected a string", from_mapping({"lazygit_cmd": 3}).warnings)
        with mock.patch("agentree.actions.exec.which", return_value=None):
            self.assertIsNone(lg.resolve_command("lazygit", "lazygit"))
        with mock.patch("agentree.actions.exec.which", return_value="/opt/homebrew/bin/lazygit"):
            self.assertEqual(lg.resolve_command("lazygit", "lazygit"), ("/opt/homebrew/bin/lazygit",))

    def test_floating_command_and_jump(self):
        w, snap, node = self.world()
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › x › lazygit")
        self.assertEqual(w.acts(), [
            ("zj", "S", "new-pane", "--tab-id", "1", "--cwd", WT.path, "--name", "lazygit",
             "--floating", "--close-on-exit", "--", LG),
            ("zj", "S", "go-to-tab-by-id", "1"), ("zj", "S", "focus-pane-id", "terminal_301"),
            ("kitty", SOCK, "focus-window", "--match", "id:5")])
        self.assertEqual(self.links.by_path, {})              # lazygit is not recorded

    def test_floating_pane_focus_read_back(self):
        w, snap, node = self.world()
        w.focus_tiled_noop = True                      # floating panes still take focus-pane-id
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertEqual(res.message, "S › x › lazygit")
        self.assertEqual(w.focused["S"], 301)
        self.assertNotIn("focus-next-pane", [a[2] for a in w.acts()])

    def test_focus_failure_reported_not_closed(self):
        w, snap, node = self.world()
        orig = w.zellij

        def z(argv, env):                              # even the floating pane refuses focus
            if argv[1] == "--session" and argv[4] == "focus-pane-id":
                return R(argv)
            return orig(argv, env)
        w.zellij = z
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertEqual((res.ok, res.close, res.refresh), (True, False, True))
        self.assertEqual(res.message, "lazygit pane created in tab x but focus failed: could not focus pane "
                                      "terminal_301 (floating pane)")

    def test_user_floating_layer_shown_still_focuses_lazygit(self):
        # lazygit is a floating target: direct focus-pane-id, the user's layer stays shown
        w, snap, node = self.world()
        w.focus_tiled_noop = True
        w.s("S")["panes"].append((9, 1, "/tmp", False))
        w.floating.add(9)
        w.show_floating("S", 9)
        w.active["S"] = 0                              # client elsewhere: the new pane is not auto-focused
        w.focused.pop("S")
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertIn(("zj", "S", "focus-pane-id", "terminal_301"), w.acts())
        self.assertNotIn("toggle-floating-panes", [a[2] for a in w.acts()])
        self.assertEqual(w.focused["S"], 301)
        self.assertTrue(w.layer[("S", 1)])

    def test_creates_tab_when_missing(self):
        w, snap, node = self.world(wt_tab=False)
        no_wt_tab(w)
        res = self.g(w, snap, row("worktree", WorktreeNode(WT), "S"), current="S")
        self.assertTrue(res.ok, res.message)
        # the floating pane opened in the (new, active) tab already has the focus: read back, no
        # focus-pane-id needed
        self.assertEqual([a[2] for a in w.acts()], ["new-tab", "new-pane", "go-to-tab-by-id"])
        self.assertEqual(w.focused["S"], 301)

    def test_not_floating_fails_verification(self):
        w, snap, node = self.world()
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            for x in w.extra.values():
                x["floating"] = False
            return r
        w.zellij = z
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("but it is not floating", res.message)

    def test_multi_client_refused(self):
        w, snap, node = self.world(clients=3)
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertIn("3 clients attached to S", res.message)
        self.assertEqual(w.acts(), [])

    def test_dry_run(self):
        w, snap, node = self.world()
        res = self.g(w, snap, row("worktree", node.worktrees[1], "S"), current="S", dry_run=True)
        self.assertTrue(res.ok)
        self.assertEqual(res.message, "dry-run: 1 command logged")
        dry = [a for a, _e, d in w.calls if d]
        self.assertEqual(dry[0][4:], ("new-pane", "--tab-id", "1", "--cwd", WT.path, "--name", "lazygit",
                                      "--floating", "--close-on-exit", "--", LG))
        self.assertEqual(w.new_pane_args, [])

    def test_real_actions_wiring(self):
        w, snap, node = self.world()
        acts = RealActions(Config(lazygit_cmd="/nonexistent/lazygit"), run_fn=w, state_dir=self.tmp.name)
        self.assertEqual(acts.lazygit(row("worktree", node.worktrees[1], "S"), snap).message, lg.MISSING)
        acts = RealActions(Config(lazygit_cmd=LG, zellij_socket_dirs=(DIR,)), dry_run=True, run_fn=w,
                           environ={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_SOCKET_DIR": DIR},
                           sockets=lambda: [SOCK], state_dir=self.tmp.name, nav_overrides=fake_nav(w, clock=lambda: 1010.0, zellij=ZJ, kitty=KT))
        res = acts.lazygit(row("worktree", node.worktrees[1], "S"), snap)
        self.assertTrue(res.ok, res.message)


if __name__ == "__main__":
    unittest.main()
