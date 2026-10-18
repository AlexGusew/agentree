"""`python3 -m agentree.ui.demo` — the popup on sample data with no real actions.

The first frame shows the sample marked stale; each refresh sleeps 0.3 s and re-stamps it.
Flags: --hide (start in hide mode), --text TEXT (start with a filter), --dump WxH (print
one plain frame and exit, no terminal needed), --fake (slow pretend actions: try the busy
status, quit-while-busy and the spawn dialog; nothing touches the system).
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import time


def fake_actions():
    """Pretend actions with realistic delays. Never runs anything."""
    from agentree.actions.api import (
        ActionResult, AgentChoice, AgentOptions, NullActions, Plan, ProgressEvent, SpawnOptions,
    )
    from agentree.model.sample import DOT, ENGINE, WEB

    from agentree.providers import registry

    agents = tuple(AgentChoice(s.id, s.label, True) for s in registry.specs())

    class FakeActions(NullActions):
        def jump(self, row, snap):
            time.sleep(1.5)
            return ActionResult(True, f"pretend jump to {row.key}")

        def agent_options(self):
            return AgentOptions(agents, agents[0].id if agents else "")

        def new_agent(self, row, snap, provider=None):
            time.sleep(1.0)
            return ActionResult(True, f"pretend {provider} agent in {row.key}")

        def spawn_options(self, row, snap):
            return SpawnOptions(repos=(WEB, DOT, ENGINE), default_repo=WEB,
                                layouts=("agent", "default"), agents=agents)

        def plan_spawn(self, req, snap):
            slug = req.branch.rsplit("/", 1)[-1] or "…"
            path = f"{req.repo}/.worktrees/{slug}"
            name = req.repo.rsplit("/", 1)[-1]
            steps = [("git", "-C", req.repo, "worktree", "add", "-b", req.branch, path,
                      "HEAD" if req.base == "head" else "origin/main")]
            if req.copy_env:
                steps.append(("cp", f"{req.repo}/.env", f"{path}/"))
            if req.run_install:
                steps.append(("pnpm", "install", "--dir", path))
            steps.append(("zellij", "--session", name if req.target == "tab" else f"{name}/{slug}", "action",
                          "new-tab", "--layout", req.layout or "default", "--cwd", path))
            errors = () if req.branch.strip() else ("type a branch name",)
            return Plan("spawn", None, tuple(steps), data={"worktree_path": path, "slug": slug,
                                                            "install_cmd": "pnpm install"}, errors=errors)

        def execute(self, plan, typed=None, *, progress=None, cancel=None):
            n = len(plan.steps)
            for i, st in enumerate(plan.steps):
                label = " ".join(st[:4])
                progress and progress(ProgressEvent(i, n, label, "running"))
                time.sleep(0.8)
                if cancel is not None and cancel.is_set():
                    progress and progress(ProgressEvent(i, n, label, "failed", "cancelled"))
                    for j in range(i - 1, -1, -1):
                        progress and progress(ProgressEvent(j, n, " ".join(plan.steps[j][:4]), "rolled_back",
                                                            "undone"))
                    return ActionResult(False, "cancelled, rolled back")
                progress and progress(ProgressEvent(i, n, label, "ok"))
            return ActionResult(True, "pretend workspace ready")

    return FakeActions()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m agentree.ui.demo")
    p.add_argument("--hide", action="store_true", help="start with worktree mode = hide")
    p.add_argument("--text", default="", help="start with this filter text")
    p.add_argument("--dump", metavar="WxH", help="print one plain-text frame and exit")
    p.add_argument("--fake", action="store_true", help="slow pretend actions (spawn dialog, busy status)")
    a = p.parse_args(argv)

    from agentree.actions.api import NullActions
    from agentree.config import Config
    from agentree.model.sample import HOME, sample_snapshot

    cfg = Config()
    snap = dataclasses.replace(sample_snapshot(time.time()), stale=True)
    ui = cfg.default_ui()
    if a.hide:
        ui = dataclasses.replace(ui, worktree_mode="hide")
    if a.text:
        ui = dataclasses.replace(ui, text=a.text)

    if a.dump:
        from agentree.ui.compose import compose, make_state, plain
        w, _, h = a.dump.partition("x")
        st = make_state(snap, ui, now=snap.taken_at, home=HOME)
        print("\n".join(plain(compose(st, (int(w), int(h))))))
        return 0

    def refresh():
        time.sleep(0.3)
        return dataclasses.replace(sample_snapshot(time.time()), stale=False)

    from agentree.ui.app import run_app
    actions = fake_actions() if a.fake else NullActions()
    return run_app(cfg=cfg, snapshot=snap, ui=ui, refresh=refresh, actions=actions,
                   save_ui=lambda _ui: None)


if __name__ == "__main__":
    sys.exit(main())
