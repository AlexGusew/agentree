"""App key handling, actions, refresh and the renderer, without a terminal."""
import os
import threading
import time
import unittest
from dataclasses import replace

from agentree.actions.api import (
    ActionResult, AgentChoice, AgentOptions, Confirm, NullActions, Plan, ProgressEvent, SpawnOptions,
)
from agentree.config import Config
from agentree.model.sample import sample_snapshot
from agentree.types import UiState
from agentree.ui.app import App
from agentree.ui.compose import compose, make_state
from agentree.ui.screen import Renderer
from agentree.ui.term import Key
from agentree.ui.widgets import (
    AgentChooserModal, CheckField, ChoiceField, ConfirmModal, FormModal, HelpModal, PromptModal, Step, TextField,
    TextInput,
)

from tests.test_model_tree import PROV_KEY, provider_snapshot

NOW = 1_800_000_000.0
SSO = "w:web-app:/Users/me/Projects/web-app/.worktrees/sso-login"


def k(name):
    return Key(name, name if len(name) == 1 else "")


class Rec(NullActions):
    def __init__(self, plan=None, close=False):
        super().__init__()
        self.calls = []
        self.plan = plan
        self.close = close

    def jump(self, row, snap):
        self.calls.append(("jump", row.key))
        return ActionResult(True, "", close=self.close)

    def lazygit(self, row, snap):
        self.calls.append(("lazygit", row.key))
        raise RuntimeError("boom")

    def rename_session(self, row, snap, new_name):
        self.calls.append(("rename", row.key, new_name))
        return ActionResult(True, f"renamed to {new_name}", refresh=True)

    def plan_remove(self, row, snap):
        self.calls.append(("plan_remove", row.key))
        return self.plan or ActionResult(False, "nope")

    def execute(self, plan, typed=None, *, progress=None, cancel=None):
        self.calls.append(("execute", plan.kind, typed))
        return ActionResult(True, "removed")


OC = AgentChoice("opencode", "opencode", True)
CC = AgentChoice("claude", "Claude Code", True)
CX = AgentChoice("codex", "Codex", True)


def gone(c):
    return replace(c, available=False)


class AgentActs(Rec):
    """Fake actions for `o`: fixed agent options; new_agent records (row, provider, snapshot).
    With `gate`, agent_options blocks until `release` is set."""

    def __init__(self, choices=(OC, CC), default="opencode", gate=False):
        super().__init__()
        self.options = AgentOptions(tuple(choices), default)
        self.gate = gate
        self.release = threading.Event()
        self.snaps = []

    def agent_options(self):
        self.calls.append(("agent_options",))
        if self.gate:
            self.release.wait(5)
        return self.options

    def new_agent(self, row, snap, provider=None):
        self.calls.append(("new_agent", row.key, provider))
        self.snaps.append(snap)
        return ActionResult(True, f"started {provider}")


APPS = []


def make_app(ui=UiState(), actions=None, refresh=None, snap=None):
    snap = snap or sample_snapshot(NOW)
    app = App(cfg=Config(), snapshot=snap, ui=ui, refresh=refresh or (lambda: snap),
              actions=actions or Rec(), home="/Users/me", size=(120, 32))
    app.render()
    APPS.append(app)
    return app


def tearDownModule():
    # release any blocked fake actions, then stop every worker this module started
    for app in APPS:
        for ev in ("release",):
            e = getattr(app.actions, ev, None)
            if isinstance(e, threading.Event):
                e.set()
    for app in APPS:
        app.worker.shutdown(1.0)
    APPS.clear()


def settle(app, timeout=2.0):
    """Pump the main-loop side until the worker is idle and no preview is due."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.drain_results()
        app._maybe_plan(time.monotonic())
        if app.worker.current is None and app.plan_due is None and app.q.empty():
            app.render()
            return
        time.sleep(0.005)
    raise AssertionError("worker did not settle")


def press(app, *names, wait=True):
    for n in names:
        app.handle(k(n))
        if wait:
            settle(app)
        app.render()


class Keys(unittest.TestCase):
    def test_move_and_fold(self):
        app = make_app()
        self.assertEqual(app.st.ui.selected, "s:web-app")
        press(app, "j")
        self.assertTrue(app.st.ui.selected.startswith("w:web-app:"))
        press(app, "j", " ")                  # sso-login is open -> fold
        self.assertEqual(app.st.ui.selected, SSO)
        self.assertIn((SSO, False), app.st.ui.folds)
        press(app, "l")                       # unfold
        self.assertIn((SSO, True), app.st.ui.folds)
        press(app, "l")                       # go to first child
        self.assertEqual(app.st.ui.selected, "a:4101")
        press(app, "h")                       # leaf -> parent
        self.assertEqual(app.st.ui.selected, SSO)
        press(app, "end")
        self.assertEqual(app.st.ui.selected, "a:4401")
        press(app, "home")
        self.assertEqual(app.st.ui.selected, "s:web-app")
        press(app, "pgdn")
        self.assertNotEqual(app.st.ui.selected, "s:web-app")

    def test_filters(self):
        app = make_app()
        press(app, "W")
        self.assertEqual(app.st.ui.worktree_mode, "with_tab")
        press(app, "A", "P", "D")
        self.assertEqual((app.st.ui.show_agents, app.st.ui.show_panes, app.st.ui.show_detached),
                         (False, True, False))

    def test_text_filter_mode(self):
        app = make_app()
        press(app, "/", "s", "s", "o", "q")
        self.assertEqual(app.st.ui.text, "ssoq")      # q is text while typing
        self.assertFalse(app.quit)
        press(app, "backspace")
        self.assertEqual(app.st.ui.text, "sso")
        app.handle(Key("paste", "x\ny"))
        self.assertEqual(app.st.ui.text, "ssox y")
        press(app, "ctrl+u")
        press(app, *"sso")
        press(app, "enter")
        self.assertEqual(app.st.mode, "normal")
        self.assertEqual(app.st.ui.text, "sso")
        press(app, "esc")                              # clears text, stays open
        self.assertEqual(app.st.ui.text, "")
        self.assertFalse(app.quit)
        press(app, "/", "z", "esc")                    # esc in filter mode: clear + leave
        self.assertEqual((app.st.mode, app.st.ui.text), ("normal", ""))
        press(app, "esc")
        self.assertTrue(app.quit)

    def test_down_leaves_filter(self):
        app = make_app()
        press(app, "/", "w", "down")
        self.assertEqual((app.st.mode, app.st.ui.text), ("normal", "w"))

    def test_quit_keys(self):
        for key in ("q", "ctrl+c"):
            app = make_app()
            press(app, key)
            self.assertTrue(app.quit)

    def test_help(self):
        app = make_app()
        press(app, "?")
        self.assertIsInstance(app.st.modal, HelpModal)
        press(app, "j")
        self.assertEqual(app.st.ui.selected, "s:web-app")   # keys go to the modal
        press(app, "esc")
        self.assertIsNone(app.st.modal)
        self.assertFalse(app.quit)

    def test_spawn_with_null_actions_shows_error(self):
        app = make_app(actions=NullActions())
        press(app, "ctrl+n")
        m = app.st.modal
        self.assertEqual(getattr(m, "kind", ""), "spawn")
        self.assertEqual(m.plan.errors, ("spawn is not available in this mode",))
        self.assertFalse(m.submit_enabled)
        press(app, "ctrl+enter")
        self.assertEqual(m.phase, "edit")
        self.assertIn("fix the problems", m.error)
        from agentree.ui.screen import plain
        text = "\n".join(plain(compose(app.st, (120, 32))))
        self.assertIn("✗ spawn is not available in this mode", text)
        press(app, "esc")
        self.assertIsNone(app.st.modal)


class Actions(unittest.TestCase):
    def test_jump_close(self):
        acts = Rec(close=True)
        app = make_app(actions=acts)
        press(app, "enter")
        self.assertEqual(acts.calls, [("jump", "s:web-app")])
        self.assertTrue(app.quit)

    def test_null_actions_status(self):
        app = make_app(ui=UiState(selected=SSO), actions=NullActions())
        press(app, "o")
        self.assertEqual((app.st.status.text, app.st.status.tone), ("No agent command found; run agentree doctor", "error"))
        self.assertFalse(app.quit)

    def test_help_lists_o_not_c(self):
        from agentree.ui.widgets import HELP
        act = dict(dict(HELP)["ACT"])
        self.assertEqual(act["o"], "agent here")
        self.assertNotIn("c", act)

    def test_action_exception_becomes_status(self):
        app = make_app()
        press(app, "g")
        self.assertEqual(app.st.status.tone, "error")
        self.assertIn("boom", app.st.status.text)

    def test_remove_with_typed_confirm(self):
        plan = Plan("remove-worktree", Confirm("Remove worktree feat/sso-login", ("$ git worktree remove x",),
                                               require_text="feat/sso-login"))
        acts = Rec(plan=plan)
        app = make_app(ui=UiState(selected=SSO), actions=acts)
        press(app, "x")
        self.assertIsInstance(app.st.modal, ConfirmModal)
        press(app, "y", "enter")                       # 'y' is typed text here; no match yet
        self.assertIsInstance(app.st.modal, ConfirmModal)
        press(app, "ctrl+u", *"feat/sso-login", "enter")
        self.assertIsNone(app.st.modal)
        self.assertEqual(acts.calls[-1], ("execute", "remove-worktree", "feat/sso-login"))
        self.assertEqual(app.st.status.text, "removed")

    def test_remove_simple_confirm_and_cancel(self):
        plan = Plan("kill-agent", Confirm("Stop agent?", ("SIGTERM 4101",)))
        acts = Rec(plan=plan)
        app = make_app(ui=UiState(selected="a:4101"), actions=acts)
        press(app, "x", "n")
        self.assertIsNone(app.st.modal)
        self.assertEqual([c[0] for c in acts.calls], ["plan_remove"])
        press(app, "x", "y")
        self.assertEqual(acts.calls[-1], ("execute", "kill-agent", None))

    def test_remove_refused(self):
        app = make_app()
        press(app, "x")
        self.assertIsNone(app.st.modal)
        self.assertEqual((app.st.status.text, app.st.status.tone), ("nope", "error"))

    def test_rename(self):
        acts = Rec()
        refreshed = []
        snap = sample_snapshot(NOW)
        app = make_app(ui=UiState(selected=SSO), actions=acts, refresh=lambda: refreshed.append(1) or snap)
        press(app, "r")
        self.assertIsInstance(app.st.modal, PromptModal)
        self.assertEqual(app.st.modal.value, "web-app")
        press(app, "ctrl+u", *"web", "enter")
        self.assertEqual(acts.calls[-1], ("rename", "s:web-app", "web"))
        self.assertEqual(app.st.status.text, "renamed to web")
        deadline = time.time() + 1
        while not refreshed and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(refreshed)                     # result.refresh triggered a refresh


# --------------------------------------------------------------------------- agent here (o)


class AgentHere(unittest.TestCase):
    """`o` = agent here for every provider; `c` is not a key any more."""

    def app(self, acts, selected=PROV_KEY):
        return make_app(ui=UiState(selected=selected), actions=acts, snap=provider_snapshot())

    def text(self, app):
        from agentree.ui.screen import plain
        app.render()
        return "\n".join(plain(compose(app.st, (120, 32))))

    def test_c_is_unbound(self):
        acts = AgentActs((OC, CC, CX))
        app = self.app(acts)
        press(app, "c")
        self.assertEqual(acts.calls, [])
        self.assertIsNone(app.st.modal)
        self.assertIsNone(app.st.status)
        self.assertFalse(app.quit)
        press(app, "/", "c")                       # text while filtering, not an action
        self.assertEqual(app.st.ui.text, "c")
        self.assertEqual(acts.calls, [])

    def test_needs_a_worktree_agent_or_pane_row(self):
        acts = AgentActs()
        app = self.app(acts, selected="s:web-app")
        press(app, "o")
        self.assertEqual((app.st.status.text, app.st.status.tone),
                         ("agent here: select a worktree, agent or pane", "error"))
        self.assertEqual(acts.calls, [])
        app = self.app(acts, selected="a:9102")    # an agent row works: same worktree
        press(app, "o", "enter")
        self.assertEqual(acts.calls[0], ("agent_options",))
        self.assertEqual(acts.calls[-1], ("new_agent", "a:9102", "opencode"))

    def test_zero_available(self):
        acts = AgentActs((gone(OC), gone(CC)))
        app = self.app(acts)
        press(app, "o")
        self.assertEqual((app.st.status.text, app.st.status.tone), ("No agent command found; run agentree doctor", "error"))
        self.assertIsNone(app.st.modal)
        self.assertEqual(acts.calls, [("agent_options",)])

    def test_one_available_launches_directly(self):
        acts = AgentActs((OC, gone(CC)), default="opencode")
        app = self.app(acts)
        press(app, "o")
        self.assertIsNone(app.st.modal)
        self.assertEqual(acts.calls, [("agent_options",), ("new_agent", PROV_KEY, "opencode")])
        self.assertEqual(app.st.status.text, "started opencode")

    def test_default_unavailable_with_one_other_installed(self):
        acts = AgentActs((gone(OC), CC), default="opencode")
        app = self.app(acts)
        press(app, "o")
        self.assertIsNone(app.st.modal)                             # no chooser for a single choice
        self.assertEqual(acts.calls, [("agent_options",), ("new_agent", PROV_KEY, "claude")])

    def test_many_available_opens_the_chooser(self):
        acts = AgentActs((OC, CC, CX), default="claude")
        app = self.app(acts)
        press(app, "o")
        m = app.st.modal
        self.assertIsInstance(m, AgentChooserModal)
        self.assertEqual([c.id for c in m.choices], ["opencode", "claude", "codex"])
        self.assertEqual(m.provider, "claude")                      # the default is preselected
        self.assertEqual(acts.calls, [("agent_options",)])
        text = self.text(app)
        for needle in ("Agent here", "opencode", "Claude Code", "Codex", "↑ ↓ choose   ↵ launch   esc cancel"):
            self.assertIn(needle, text)
        press(app, "down")
        self.assertEqual(m.provider, "codex")
        press(app, "j")                                              # clamps at the end
        self.assertEqual(m.provider, "codex")
        press(app, "k", "up")
        self.assertEqual(m.provider, "opencode")
        press(app, "up")                                             # clamps at the top
        self.assertEqual(m.provider, "opencode")
        press(app, "j")
        self.assertEqual(app.st.ui.selected, PROV_KEY)               # keys went to the chooser
        press(app, "enter")
        self.assertIsNone(app.st.modal)
        self.assertEqual(acts.calls, [("agent_options",), ("new_agent", PROV_KEY, "claude")])
        self.assertEqual(app.st.status.text, "started claude")

    def test_chooser_lists_only_available_and_skips_an_unavailable_default(self):
        acts = AgentActs((OC, gone(CC), CX), default="claude")
        app = self.app(acts)
        press(app, "o")
        m = app.st.modal
        self.assertEqual([c.id for c in m.choices], ["opencode", "codex"])
        self.assertEqual(m.provider, "opencode")                    # first available
        self.assertNotIn("Claude Code", self.text(app))
        press(app, "down", "enter")
        self.assertEqual(acts.calls[-1], ("new_agent", PROV_KEY, "codex"))

    def test_cancel_does_nothing(self):
        for key in ("esc", "q"):
            acts = AgentActs((OC, CC))
            app = self.app(acts)
            press(app, "o")
            self.assertIsInstance(app.st.modal, AgentChooserModal)
            press(app, "down", key)
            self.assertIsNone(app.st.modal)
            self.assertEqual(acts.calls, [("agent_options",)])
            self.assertFalse(app.quit, key)
            self.assertIsNone(app.st.status)
            self.assertIsNone(app.worker.current)

    def test_launch_target_is_captured_when_o_is_pressed(self):
        acts = AgentActs((OC, gone(CC)), gate=True)
        app = self.app(acts)
        snap0 = app.st.snapshot
        press(app, "o", wait=False)
        self.assertFalse(app.busy)                                  # quiet lookup: no spinner
        press(app, "home", "j", "j", wait=False)                   # selection moves meanwhile
        self.assertNotEqual(app.st.ui.selected, PROV_KEY)
        app.st.snapshot = replace(provider_snapshot(), taken_at=NOW + 5)   # and a refresh lands
        acts.release.set()
        settle(app)
        self.assertEqual(acts.calls, [("agent_options",), ("new_agent", PROV_KEY, "opencode")])
        self.assertIs(acts.snaps[0], snap0)

    def test_chooser_keeps_the_captured_target(self):
        acts = AgentActs((OC, CC), gate=True)
        app = self.app(acts)
        snap0 = app.st.snapshot
        press(app, "o", wait=False)
        press(app, "j", wait=False)
        app.st.snapshot = replace(provider_snapshot(), taken_at=NOW + 5)
        acts.release.set()
        settle(app)
        press(app, "enter")
        self.assertEqual(acts.calls[-1], ("new_agent", PROV_KEY, "opencode"))
        self.assertIs(acts.snaps[0], snap0)

    def test_refused_while_busy(self):
        acts = AgentActs((OC, CC), gate=True)
        app = self.app(acts)
        press(app, "o", wait=False)
        press(app, "o", wait=False)                                # lookup still running
        self.assertEqual(app.st.status.text, "busy — wait")
        acts.release.set()
        settle(app)
        self.assertEqual(acts.calls, [("agent_options",)])

    def test_options_failure_is_a_status(self):
        class Broken(AgentActs):
            def agent_options(self):
                raise RuntimeError("boom")
        app = self.app(Broken())
        press(app, "o")
        self.assertEqual((app.st.status.text, app.st.status.tone), ("agent here: boom", "error"))
        self.assertIsNone(app.st.modal)


# --------------------------------------------------------------------------- worker


class Gate(Rec):
    """Actions whose jump blocks until released; execute reports progress."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.release = threading.Event()
        self.threads = set()
        self.snaps = []

    def jump(self, row, snap):
        self.threads.add(threading.current_thread().name)
        self.snaps.append(snap)
        self.calls.append(("jump", row.key))
        self.release.wait(5)
        return ActionResult(True, "jumped", close=self.close)

    def agent_options(self):
        return AgentOptions((OC, gone(CC)), "opencode")

    def new_agent(self, row, snap, provider=None):
        self.calls.append(("new_agent", row.key))
        return ActionResult(True, "agent started")

    def execute(self, plan, typed=None, *, progress=None, cancel=None):
        self.threads.add(threading.current_thread().name)
        self.calls.append(("execute", plan.kind, typed))
        for i, label in enumerate(("close tab", "git worktree remove")):
            progress(ProgressEvent(i, 2, label, "running"))
            if i == 1:
                self.release.wait(5)
            progress(ProgressEvent(i, 2, label, "ok"))
        return ActionResult(True, "removed", refresh=True)


def wait_for(pred, timeout=2.0, app=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app is not None:
            app.drain_results()
        if pred():
            return True
        time.sleep(0.005)
    return False


class Worker(unittest.TestCase):
    def test_actions_run_off_the_main_thread_one_at_a_time(self):
        acts = Gate()
        app = make_app(actions=acts)
        snap_at_dispatch = app.st.snapshot
        press(app, "enter", wait=False)
        self.assertTrue(app.busy)
        app.render()
        bar = "".join(app.canvas.chars[-1])
        self.assertIn("working… jump", bar)
        self.assertTrue(bar.rstrip().endswith("q q force quit"))      # not "esc close" while busy
        # navigation and filters keep working while busy
        press(app, "j", "W", wait=False)
        self.assertNotEqual(app.st.ui.selected, "s:web-app")
        self.assertEqual(app.st.ui.worktree_mode, "with_tab")
        # a second action is refused
        press(app, "o", wait=False)
        self.assertEqual(app.st.status.text, "busy — wait")
        self.assertNotIn(("new_agent",), [c[:1] for c in acts.calls])
        # refreshes still land while the action runs, but the action got the dispatch-time snapshot
        app.st.snapshot = replace(sample_snapshot(NOW + 5), taken_at=NOW + 5)
        acts.release.set()
        self.assertTrue(wait_for(lambda: not app.worker.busy, app=app))
        self.assertEqual(acts.threads, {"agentree-action"})
        self.assertIs(acts.snaps[0], snap_at_dispatch)
        self.assertEqual((app.st.status.text, app.st.status.tone), ("jumped", "ok"))
        app.render()
        self.assertIsNone(app.st.activity)
        self.assertTrue("".join(app.canvas.chars[-1]).rstrip().endswith("esc close"))
        press(app, "o")
        self.assertEqual(acts.calls[-1][0], "new_agent")

    def test_result_close_exits(self):
        acts = Gate(close=True)
        acts.release.set()
        app = make_app(actions=acts)
        press(app, "enter")
        self.assertTrue(app.quit)

    def test_quit_while_busy_needs_second_q(self):
        acts = Gate()
        app = make_app(actions=acts)
        press(app, "enter", wait=False)
        press(app, "esc", wait=False)
        self.assertFalse(app.quit)
        self.assertEqual(app.st.status.text, "action running — press q again to quit anyway")
        press(app, "q", wait=False)
        self.assertTrue(app.quit)
        acts.release.set()

    def test_quit_arming_resets_after_action(self):
        acts = Gate()
        app = make_app(actions=acts)
        press(app, "enter", wait=False)
        press(app, "q", wait=False)
        self.assertFalse(app.quit)
        acts.release.set()
        self.assertTrue(wait_for(lambda: not app.worker.busy, app=app))
        self.assertFalse(app.quit_armed)
        press(app, "q")                                # nothing running: quits at once
        self.assertTrue(app.quit)

    def test_confirmed_execute_shows_progress_in_status(self):
        plan = Plan("remove-worktree", Confirm("Remove worktree?", ("$ git worktree remove x",)))
        acts = Gate(plan=plan)
        app = make_app(ui=UiState(selected=SSO), actions=acts)
        press(app, "x")
        self.assertIsInstance(app.st.modal, ConfirmModal)
        press(app, "y", wait=False)
        self.assertTrue(wait_for(lambda: "2/2" in (app.worker.current.note if app.worker.current else ""),
                                 app=app))
        app.render()
        bar = "".join(compose(app.st, (120, 32)).chars[-1])
        self.assertIn("working… remove-worktree · 2/2 git worktree remove", bar)
        acts.release.set()
        self.assertTrue(wait_for(lambda: not app.worker.busy, app=app))
        self.assertEqual(app.st.status.text, "removed")
        self.assertEqual(acts.threads, {"agentree-action"})

    def test_action_exception_is_marshalled(self):
        app = make_app()
        press(app, "g")
        self.assertEqual(app.st.status.tone, "error")
        self.assertIn("lazygit: boom", app.st.status.text)


# --------------------------------------------------------------------------- spawn

WEB = "/Users/me/Projects/web-app"
API = "/Users/me/Projects/api"


class SpawnActs(Rec):
    def __init__(self, fail_at=None):
        super().__init__()
        self.plans = []
        self.release = threading.Event()
        self.cancel_seen = None
        self.fail_at = fail_at

    def spawn_options(self, row, snap):
        return SpawnOptions(repos=(API, WEB), default_repo=WEB, layouts=("agent", "default"))

    def plan_spawn(self, req, snap):
        self.plans.append(req)
        slug = req.branch.rsplit("/", 1)[-1]
        errors = () if req.branch else ("branch name is empty",)
        if req.branch.startswith("main"):
            errors = ("branch main already exists",)
        steps = (("git", "-C", req.repo, "worktree", "add", "-b", req.branch, f"{req.repo}/.worktrees/{slug}",
                  "origin/main"),
                 ("cp", f"{req.repo}/.env", f"{req.repo}/.worktrees/{slug}/"),
                 ("zellij", "--session", "web-app", "action", "new-tab", "--layout", req.layout or "default"))
        return Plan("spawn", None, steps, data={"worktree_path": f"{req.repo}/.worktrees/{slug}",
                                                 "slug": slug, "install_cmd": "pnpm install"},
                    warnings=("no .env files in web-app",) if req.copy_env else (), errors=errors)

    def execute(self, plan, typed=None, *, progress=None, cancel=None):
        self.calls.append(("execute", plan.kind))
        n = len(plan.steps)
        for i in range(n):
            progress(ProgressEvent(i, n, f"step {i + 1}", "running"))
            if i == 1:
                self.release.wait(5)
                if cancel is not None and cancel.is_set():
                    self.cancel_seen = True
                    progress(ProgressEvent(i, n, f"step {i + 1}", "failed", "cancelled"))
                    progress(ProgressEvent(0, n, "step 1", "rolled_back", "worktree removed"))
                    return ActionResult(False, "cancelled, rolled back", refresh=True)
            progress(ProgressEvent(i, n, f"step {i + 1}", "ok", "done" if i == 0 else ""))
        return ActionResult(True, "workspace ready", close=True, refresh=True)


class PrefixActs(SpawnActs):
    """Options carry a branch prefix; plans report the session the backend would use."""

    def spawn_options(self, row, snap):
        return SpawnOptions(repos=(API, WEB), default_repo=WEB, layouts=("agent",), branch_prefix="agent/")

    def plan_spawn(self, req, snap):
        p = super().plan_spawn(req, snap)
        from agentree.ui.spawn import slugify
        slug = slugify(req.branch)
        session = f"wa-{slug}-7" if req.target == "session" else "web-app-main"
        return replace(p, data={**p.data, "slug": slug, "session": session}, errors=())


def open_spawn(acts):
    app = make_app(actions=acts)
    press(app, "ctrl+n")
    return app, app.st.modal


class Spawn(unittest.TestCase):
    def text(self, app, size=(120, 32)):
        from agentree.ui.screen import plain
        app.render()
        return "\n".join(plain(compose(app.st, size)))

    def test_form_fields_and_initial_plan(self):
        acts = SpawnActs()
        app, m = open_spawn(acts)
        self.assertEqual(m.kind, "spawn")
        self.assertEqual(m.field("repo").display, "web-app")       # default repo, shown as basename
        self.assertEqual(len(acts.plans), 1)
        self.assertEqual(m.plan.errors, ("branch name is empty",))
        self.assertFalse(m.submit_enabled)
        text = self.text(app)
        for s in ("New workspace", "repo", "‹ web-app ›", "‹ origin/main ›", "new tab in web-app's session",
                  "‹ agent ›", "[x] copy .env* from root", "[ ] run install (pnpm install)",
                  "[x] switch to it after", "✗ branch name is empty", "type a branch name"):
            self.assertIn(s, text)

    def agent_acts(self, agents, default="opencode"):
        class Acts(SpawnActs):
            def spawn_options(self, row, snap):
                return SpawnOptions(repos=(API, WEB), default_repo=WEB, layouts=("agent", "default"),
                                    agents=agents, default_provider=default)
        return Acts()

    def test_agent_field(self):
        acts = self.agent_acts((OC, CC))
        app, m = open_spawn(acts)
        f = m.field("agent")
        self.assertEqual((f.label, f.options, f.value), ("agent", ("opencode", "claude"), "opencode"))
        self.assertEqual(acts.plans[-1].agent, "opencode")
        self.assertIn("optional first message for the agent", self.text(app))
        names = [x.name for x in m.fields]
        self.assertEqual(names.index("agent"), names.index("layout") + 1)
        m.focus = names.index("agent")
        press(app, "right")
        self.assertEqual(acts.plans[-1].agent, "claude")
        self.assertIn("‹ Claude Code ›", self.text(app))           # label from the options, not a table

    def test_agent_default_from_options(self):
        acts = self.agent_acts((OC, CC), default="claude")
        app, m = open_spawn(acts)
        self.assertEqual(m.field("agent").value, "claude")
        self.assertEqual(acts.plans[-1].agent, "claude")
        acts = self.agent_acts((OC, CC), default="nope")           # unknown default: first choice
        app, m = open_spawn(acts)
        self.assertEqual(m.field("agent").value, "opencode")

    def test_agent_third_provider_and_not_found(self):
        acts = self.agent_acts((OC, gone(CC), CX), default="codex")
        app, m = open_spawn(acts)
        f = m.field("agent")
        self.assertEqual(f.options, ("opencode", "claude", "codex"))
        self.assertEqual(f.labels, ("opencode", "Claude Code (not found)", "Codex"))
        self.assertEqual((f.value, acts.plans[-1].agent), ("codex", "codex"))
        self.assertIn("‹ Codex ›", self.text(app))
        m.focus = [x.name for x in m.fields].index("agent")
        press(app, "left")                                         # not found, still selectable
        self.assertEqual(acts.plans[-1].agent, "claude")
        self.assertIn("‹ Claude Code (not found) ›", self.text(app))

    def test_no_agent_choices_never_become_opencode(self):
        acts = self.agent_acts(())
        app, m = open_spawn(acts)
        f = m.field("agent")
        self.assertEqual((f.options, f.value), ((), ""))
        self.assertEqual(acts.plans[-1].agent, "")
        self.assertIn("‹ none found ›", self.text(app))

    def test_plan_recomputed_on_edit_debounced(self):
        acts = SpawnActs()
        app, m = open_spawn(acts)
        for c in "feat/x":
            app.handle(k(c))                    # fast typing: one plan after the pause
        self.assertIsNotNone(app.plan_due)
        settle(app)
        self.assertEqual(len(acts.plans), 2)
        self.assertEqual(acts.plans[-1].branch, "feat/x")
        self.assertEqual(acts.plans[-1].repo, WEB)
        self.assertTrue(m.submit_enabled)
        text = self.text(app)
        self.assertIn("→ ~/Projects/web-app/.worktrees/x", text)
        self.assertIn("$ git -C /Users/me/Projects/web-app worktree add -b feat/x", text)
        self.assertIn("! no .env files in web-app", text)
        # changing a choice re-plans too
        press(app, "up", "up", "left")        # focus repo, previous option
        self.assertEqual(acts.plans[-1].repo, API)
        press(app, "down", "down", "down", "right")   # target -> new session
        self.assertEqual(acts.plans[-1].target, "session")
        self.assertIn("new session api-x", self.text(app))   # no plan session: <repo>-<slug> fallback
        self.assertNotIn("new session api/", self.text(app))

    def test_branch_prefix_prefilled_cursor_at_end(self):
        acts = PrefixActs()
        app, m = open_spawn(acts)
        self.assertEqual(m.focus, 2)
        branch = m.inputs["branch"]
        self.assertEqual((branch.value, branch.pos), ("agent/", len("agent/")))
        self.assertEqual(acts.plans[-1].branch, "agent/")        # first preview uses the prefix
        press(app, *"Fix-Login", wait=False); settle(app)
        self.assertEqual(acts.plans[-1].branch, "agent/Fix-Login")
        self.assertIn("agent/Fix-Login", self.text(app))

    def test_target_label_from_plan_session(self):
        acts = PrefixActs()
        app, m = open_spawn(acts)
        press(app, *"Fix Login", wait=False); settle(app)
        # before a session plan: fallback <repo>-<slug>, case kept, unsafe chars -> '-'
        self.assertEqual(m.field("target").labels, ("new tab in web-app-main", "new session web-app-Fix-Login"))
        self.assertEqual(m.slug(), "Fix-Login")
        press(app, "down", "right")                              # target -> session, re-plan
        self.assertEqual(acts.plans[-1].target, "session")
        self.assertIn("‹ new session wa-Fix-Login-7 ›", self.text(app))   # plan.data["session"] wins
        # the tab option names the plan's session once a tab plan arrives
        press(app, "right")
        self.assertIn("‹ new tab in web-app-main ›", self.text(app))

    def test_fallback_slug_matches_backend(self):
        from agentree.ui.spawn import session_name, slugify
        for branch, slug in (("agent/Fix-Login", "Fix-Login"), ("feat/a b+c", "a-b-c"), ("x/", "x"),
                             ("feat/.hidden-", "hidden"), ("", "")):
            self.assertEqual(slugify(branch), slug, branch)
        self.assertEqual(session_name("web-app", "Fix-Login"), "web-app-Fix-Login")
        self.assertEqual(session_name("my repo", "x"), "my-repo-x")
        try:                                                      # same rule as the backend, if present
            from agentree.actions.spawn import session_name_for, slug_of
        except Exception:                                         # noqa: BLE001 - other lane mid-edit
            return
        for branch in ("agent/Fix-Login", "feat/a b+c", "x/", "feat/.hidden-", "Ünïcode/naïve slug"):
            self.assertEqual(slugify(branch), slug_of(branch), branch)
        self.assertEqual(session_name("web-app", "a-b"), session_name_for("/p/web-app", "a-b"))

    def test_errors_disable_submit(self):
        acts = SpawnActs()
        app, m = open_spawn(acts)
        press(app, *"main2", wait=False); settle(app)
        self.assertEqual(m.plan.errors, ("branch main already exists",))
        press(app, "ctrl+enter")
        self.assertEqual(m.phase, "edit")
        self.assertNotIn(("execute", "spawn"), acts.calls)
        self.assertIn("✗ branch main already exists", self.text(app))

    def test_submit_while_preview_stale_waits_then_runs(self):
        acts = SpawnActs()
        acts.release.set()
        app, m = open_spawn(acts)
        for c in "feat/y":
            app.handle(k(c))
        app.handle(k("ctrl+enter"))             # preview not ready yet
        self.assertTrue(m.submit_pending)
        settle(app)
        self.assertEqual(acts.calls[-1], ("execute", "spawn"))
        self.assertEqual(acts.plans[-1].branch, "feat/y")
        self.assertEqual(m.phase, "done")

    def test_progress_view_and_close(self):
        acts = SpawnActs()
        app, m = open_spawn(acts)
        press(app, *"feat/x", wait=False); settle(app)
        press(app, "ctrl+s", wait=False)
        self.assertEqual(m.phase, "running")
        self.assertTrue(wait_for(lambda: m.progress[1].state == "running", app=app))
        text = self.text(app)
        self.assertIn("PROGRESS", text)
        self.assertIn("✓ step 1", text)
        self.assertIn("done", text)
        self.assertIn("· zellij --session web-app action new-tab --layout agent", text)  # pending: plan label
        self.assertIn("esc cancel and roll back", text)
        self.assertIn("working… spawn", text)
        acts.release.set()
        self.assertTrue(wait_for(lambda: m.phase == "done", app=app))
        settle(app)
        text = self.text(app)
        self.assertIn("✓ workspace ready", text)
        self.assertIn("✓ step 3", text)
        self.assertFalse(app.quit)               # close waits for the user
        press(app, "enter")
        self.assertIsNone(app.st.modal)
        self.assertTrue(app.quit)                # result.close

    def test_cancel_sets_event_and_shows_rollback(self):
        acts = SpawnActs()
        app, m = open_spawn(acts)
        press(app, *"feat/x", wait=False); settle(app)
        press(app, "ctrl+enter", wait=False)
        self.assertTrue(wait_for(lambda: m.progress[1].state == "running", app=app))
        press(app, "esc", wait=False)
        self.assertTrue(m.cancel.is_set())
        self.assertIn("cancelling… rolling back", self.text(app))
        self.assertIsNotNone(app.st.modal)       # stays open while rolling back
        acts.release.set()
        self.assertTrue(wait_for(lambda: m.phase == "done", app=app))
        settle(app)
        self.assertTrue(acts.cancel_seen)
        text = self.text(app)
        self.assertIn("↺ step 1", text)
        self.assertIn("worktree removed", text)
        self.assertIn("✗ cancelled, rolled back", text)
        press(app, "esc")
        self.assertIsNone(app.st.modal)
        self.assertFalse(app.quit)

    def test_spawn_refused_while_busy(self):
        acts = Gate()
        app = make_app(actions=acts)
        press(app, "enter", wait=False)
        press(app, "ctrl+n", wait=False)
        self.assertIsNone(app.st.modal)
        self.assertEqual(app.st.status.text, "busy — wait")
        acts.release.set()
        settle(app)

    def test_spawn_module_is_lazy(self):
        import subprocess
        import sys
        out = subprocess.run([sys.executable, "-c", "import sys, agentree.ui.app; print('agentree.ui.spawn' in sys.modules)"],
                             capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


class Refresh(unittest.TestCase):
    def test_background_refresh_keeps_selection(self):
        calls = []

        def refresh():
            calls.append(1)
            time.sleep(0.05)
            return replace(sample_snapshot(NOW + 60), taken_at=NOW + 60)

        app = make_app(ui=UiState(selected="a:4102"), refresh=refresh)
        app.start_refresh()
        app.start_refresh()                  # never overlaps: queued instead
        self.assertTrue(app.st.refreshing)
        self.assertIn("↻", "".join(compose(app.st, (120, 32)).chars[0]))
        deadline = time.time() + 2
        while len(calls) < 2 and time.time() < deadline:
            app.drain_results()
            time.sleep(0.01)
        while app.refreshing and time.time() < deadline:
            app.drain_results()
            time.sleep(0.01)
        self.assertEqual(len(calls), 2)
        self.assertEqual(app.st.snapshot.taken_at, NOW + 60)
        self.assertEqual(app.st.ui.selected, "a:4102")
        self.assertFalse(app.st.refreshing)

    def test_refresh_error(self):
        def refresh():
            raise OSError("zellij went away")

        app = make_app(refresh=refresh)
        app.start_refresh()
        deadline = time.time() + 2
        while app.refreshing and time.time() < deadline:
            app.drain_results()
            time.sleep(0.01)
        self.assertEqual(app.st.status.tone, "error")
        self.assertIn("zellij went away", app.st.status.text)


class Widgets(unittest.TestCase):
    def test_text_input(self):
        t = TextInput("hello world")
        for n in ("ctrl+w",):
            t.handle(k(n))
        self.assertEqual(t.value, "hello ")
        t.handle(k("home"))
        t.handle(k(">"))
        self.assertEqual(t.value, ">hello ")
        t.handle(k("end"))
        t.handle(k("backspace"))
        self.assertEqual(t.value, ">hello")

    def test_form(self):
        f = FormModal("New workspace", [
            ChoiceField("repo", "repo", ("web-app", "api")),
            TextField("branch", "branch", hint=lambda v: f"wt/{v['branch']}"),
            CheckField("install", "run install"),
        ], preview=lambda v: [f"$ git worktree add -b {v['branch']}"])
        self.assertEqual(f.values(), {"repo": "web-app", "branch": "", "install": False})
        f.handle(k("right"))
        f.handle(k("tab"))
        for c in "feat/x":
            f.handle(k(c))
        f.handle(k("tab"))
        f.handle(k(" "))
        self.assertEqual(f.values(), {"repo": "api", "branch": "feat/x", "install": True})
        st = make_state(sample_snapshot(NOW), UiState(), now=NOW, home="/Users/me")
        st.modal = f
        from agentree.ui.screen import plain
        text = "\n".join(plain(compose(st, (120, 32))))
        for s in ("New workspace", "‹ api ›", "feat/x", "→ wt/feat/x", "[x] run install",
                  "$ git worktree add -b feat/x", "^↵ create"):
            self.assertIn(s, text)
        self.assertEqual(f.handle(k("ctrl+enter")), "ok")
        f.progress = [Step("git worktree add", "ok"), Step("pnpm install", "failed", "exit 1")]
        text = "\n".join(plain(compose(st, (120, 32))))
        self.assertIn("✓ git worktree add", text)
        self.assertIn("✗ pnpm install", text)
        self.assertIn("exit 1", text)
        self.assertIsNone(f.handle(k("esc")))   # read-only while running
        f.done = True
        self.assertEqual(f.handle(k("esc")), "cancel")


class RendererDiff(unittest.TestCase):
    def test_only_changed_lines(self):
        st = make_state(sample_snapshot(NOW), UiState(), now=NOW, home="/Users/me")
        r = Renderer(st.theme.sgr)
        first = r.render(compose(st, (100, 20)))
        self.assertIn("\x1b[2J", first)
        self.assertEqual(r.render(compose(st, (100, 20))), "")
        st.ui = replace(st.ui, selected="s:dotfiles")
        out = r.render(compose(st, (100, 20)))
        self.assertNotIn("\x1b[2J", out)
        self.assertLess(out.count("\x1b[") , first.count("\x1b["))
        self.assertGreater(out.count("H"), 1)
        self.assertIn("\x1b[2J", r.render(compose(st, (101, 20))))   # resize -> full repaint


class LifecycleTest(unittest.TestCase):
    """run_app on a real pty, in-process: wake pipe and refresh worker lifecycle."""

    def _cycle(self, refresh):
        import os
        import threading

        from agentree.ui import app as appmod

        master, slave = os.openpty()
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    if not os.read(master, 65536):
                        return
                except OSError:
                    return

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        os.write(master, b"q")
        try:
            rc = appmod.run_app(cfg=Config(), snapshot=sample_snapshot(NOW), ui=UiState(), refresh=refresh,
                                actions=NullActions(), save_ui=lambda ui: None, tty_in=slave, tty_out=slave)
        finally:
            stop.set()
            os.close(slave)
            os.close(master)
            t.join(1)
        return rc

    def test_fds_stable_when_quitting_during_refresh(self):
        import os
        import threading

        release = threading.Event()
        started = []

        def slow_refresh():
            started.append(1)
            release.wait(5)
            return sample_snapshot(NOW)

        self._cycle(slow_refresh)            # warm-up (lazy imports, first-time allocations)
        release.set()
        time.sleep(0.05)
        release.clear()
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):
            self.assertEqual(self._cycle(slow_refresh), 0)
        after_runs = len(os.listdir("/dev/fd"))
        # the refresh workers are still blocked: both wake fds were closed anyway
        self.assertEqual(after_runs, before)
        release.set()                        # late workers must tolerate the closed pipe
        time.sleep(0.1)
        self.assertEqual(len(os.listdir("/dev/fd")), before)
        self.assertGreaterEqual(len(started), 5)

    def test_wake_after_close_is_noop(self):
        app = make_app()
        import os
        app.wake_r, app.wake_w = os.pipe()
        app.wake(b"r")
        app.close_wake_pipe()
        self.assertEqual((app.wake_r, app.wake_w), (-1, -1))
        app.wake(b"r")                       # no error, no write to a recycled fd
        app.on_winch()
        app.close_wake_pipe()                # idempotent


if __name__ == "__main__":
    unittest.main()
