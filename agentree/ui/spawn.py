"""Spawn dialog (^n): the generic form wired to Actions.spawn_options / plan_spawn / execute.

Imported lazily on first ^n so it costs nothing at startup. The modal only holds state;
the app runs every Actions call on its worker and feeds results back in through
`set_plan`, `start_run`, `on_progress` and `finish`.
"""
from __future__ import annotations

import os
import re
import shlex
import threading

from agentree.actions.api import ActionResult, Plan, ProgressEvent, SpawnOptions, SpawnRequest

from .term import Key
from .widgets import CheckField, ChoiceField, FormModal, Step, TextField

PATH_KEYS = ("worktree_path", "path", "worktree")


def step_text(step) -> str:
    """Plan.steps entries are display lines or argv tuples."""
    if isinstance(step, str):
        return step
    if len(step) == 1:
        return step[0]
    return shlex.join(step)


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify(branch: str) -> str:
    """Fallback slug until a plan arrives; same rule as the backend (actions.spawn.slug_of):
    last path component, runs of chars outside [A-Za-z0-9._-] -> '-', case kept."""
    s = branch.strip().rstrip("/").rsplit("/", 1)[-1]
    return _UNSAFE.sub("-", s).strip("-.")


def session_name(repo_name: str, slug: str) -> str:
    """Fallback new-session name: `<repo>-<slug>` (zellij rejects '/' in session names)."""
    return _UNSAFE.sub("-", f"{repo_name}-{slug}")


class SpawnModal(FormModal):
    kind = "spawn"

    def __init__(self, options: SpawnOptions, *, home: str | None = None) -> None:
        self.options = options
        self.home = home
        repos = tuple(options.repos)
        idx = repos.index(options.default_repo) if options.default_repo in repos else 0
        bases = options.bases or ("origin/main", "HEAD")
        targets = tuple(options.targets) or ("tab", "session")
        layouts = tuple(options.layouts)
        choices = tuple(options.agents)
        agents = tuple(c.id for c in choices)
        agent_labels = tuple(c.label if c.available else f"{c.label} (not found)" for c in choices)
        aidx = agents.index(options.default_provider) if options.default_provider in agents else 0
        fields = [
            ChoiceField("repo", "repo", repos, idx, labels=tuple(os.path.basename(r.rstrip("/")) for r in repos),
                        empty="no repos found"),
            ChoiceField("base", "base", ("origin", "head"), 0, labels=(bases[0], "current HEAD")),
            TextField("branch", "branch", value=options.branch_prefix or "",
                      placeholder="feat/my-change", hint=self._path_hint),
            ChoiceField("target", "target", targets, 0),
            ChoiceField("layout", "layout", layouts, 0, empty="default"),
            ChoiceField("agent", "agent", agents, aidx, labels=agent_labels, empty="none found"),
            TextField("prompt", "prompt", placeholder="optional first message for the agent"),
            CheckField("copy_env", "copy .env* from root", True),
            CheckField("run_install", "run install", False),
            CheckField("switch_after", "switch to it after", True),
        ]
        super().__init__("New workspace", fields, submit_label="create")
        self.focus = 2                         # start on the branch name
        self.plan: Plan | None = None
        self.plan_req: SpawnRequest | None = None
        self.phase = "edit"                    # edit | running | done
        self.submit_pending = False            # ^↵ pressed while the preview was updating
        self.cancel = threading.Event()
        self.result: ActionResult | None = None
        self.submit_enabled = False            # until a plan without errors arrives
        self._sync()

    # ------------------------------------------------------------------ request / plan
    def request(self) -> SpawnRequest:
        v = self.values()
        return SpawnRequest(
            repo=v["repo"], branch=v["branch"].strip(), base=v["base"] or "origin",
            target=v["target"] or "tab", layout=v["layout"] or None, prompt=v["prompt"].strip(),
            copy_env=v["copy_env"], run_install=v["run_install"], switch_after=v["switch_after"],
            agent=v["agent"])

    @property
    def stale(self) -> bool:
        return self.plan is None or self.plan_req != self.request()

    @property
    def ready(self) -> bool:
        """A plan for exactly the current request, with no errors."""
        return not self.stale and self.plan is not None and not self.plan.errors

    def set_plan(self, req: SpawnRequest, plan: Plan) -> None:
        self.plan, self.plan_req = plan, req
        self.submit_enabled = not plan.errors
        if plan.errors:
            self.submit_pending = False
        self._sync()

    def _repo_name(self) -> str:
        f = self.field("repo")
        return f.display if f.options else "repo"

    def _fresh(self) -> dict:
        """plan.data, only when the plan matches the current form values."""
        return self.plan.data if self.plan is not None and not self.stale else {}

    def slug(self) -> str:
        data = self._fresh()
        return data.get("slug") or slugify(self.inputs["branch"].value)

    def _sync(self) -> None:
        repo = self._repo_name()
        data = self._fresh()
        target = self.plan_req.target if data and self.plan_req else None
        sess = data.get("session") if isinstance(data.get("session"), str) else None
        slug = self.slug()
        fallback = session_name(repo, slug) if slug else f"{repo}-…"
        new_session = sess if target == "session" and sess else fallback
        tab_label = f"new tab in {sess}" if target == "tab" and sess else f"new tab in {repo}'s session"
        t = self.field("target")
        t.labels = tuple(tab_label if o == "tab" else f"new session {new_session}" if o == "session" else o
                         for o in t.options)
        install = self.plan.data.get("install_cmd") if self.plan else None
        self.field("run_install").label = f"run install ({install})" if install else "run install"

    def _path_hint(self, values: dict) -> str:
        if not values.get("branch", "").strip():
            return "type a branch name"
        path = None
        if self.plan is not None:
            path = next((self.plan.data[k] for k in PATH_KEYS if isinstance(self.plan.data.get(k), str)), None)
        if not path:
            return "…"
        if self.home and (path == self.home or path.startswith(self.home.rstrip("/") + "/")):
            path = "~" + path[len(self.home.rstrip("/")):]
        return path + ("" if not self.stale else "  …")

    # ------------------------------------------------------------------ keys
    def handle(self, key: Key) -> str | None:
        n = key.name
        if self.phase == "running":
            if n == "esc" and not self.cancel.is_set():
                self.cancel.set()
                self.footer = ("cancelling… rolling back", "accent")
            elif n == "q":
                return "quit"
            return None
        if self.phase == "done":
            if n in ("enter", "esc", "ctrl+enter"):
                return "close"
            if n == "q":
                return "quit"
            return None
        if n in ("ctrl+enter", "ctrl+s"):
            if self.stale:
                return "ok"                    # the app waits for the fresh preview, then runs
            if self.plan is not None and self.plan.errors:
                self.error = "fix the problems above first"
                return None
            return "ok"
        before = self.values()
        res = super().handle(key)
        if self.values() != before:
            self.submit_pending = False
            self.error = ""
            self._sync()
            return "changed" if res is None else res
        return res

    # ------------------------------------------------------------------ run
    def start_run(self) -> None:
        assert self.plan is not None
        self.phase = "running"
        self.progress = [Step(step_text(s)) for s in self.plan.steps]
        self.footer = None
        self.error = ""

    def on_progress(self, ev: ProgressEvent) -> None:
        steps = self.progress if self.progress is not None else []
        while len(steps) <= ev.index:
            steps.append(Step(""))
        old = steps[ev.index]
        steps[ev.index] = Step(ev.label or old.label, ev.state, ev.detail)
        self.progress = steps

    def finish(self, res: ActionResult) -> None:
        self.phase = "done"
        self.done = True
        self.result = res
        msg = res.message or ("done" if res.ok else "failed")
        if self.cancel.is_set() and not res.ok:
            msg = res.message or "cancelled, rolled back"
        self.footer = (("✓ " if res.ok else "✗ ") + msg, "green" if res.ok else "red")

    # ------------------------------------------------------------------ content
    def preview_lines(self) -> list[tuple[str, str]]:
        if self.plan is None:
            return [("working out the commands…", "dim")]
        out = [("$ " + step_text(s) if not isinstance(s, str) and len(s) > 1 else step_text(s), "text")
               for s in self.plan.steps]
        out += [("! " + w, "accent") for w in self.plan.warnings]
        out += [("✗ " + e, "red") for e in self.plan.errors]
        return out

    @property
    def preview_title(self) -> str:  # type: ignore[override]
        return "COMMANDS · updating…" if self.stale and self.plan is not None else "COMMANDS"

    @preview_title.setter
    def preview_title(self, _v: str) -> None:
        pass

    def summary(self) -> list[tuple[str, str]]:
        req = self.plan_req or self.request()
        line = f"{self._repo_name()}  ·  {req.branch}"
        hint = self._path_hint({"branch": req.branch})
        out = [(line, "text")]
        if hint and hint != "…":
            out.append(("→ " + hint, "dim"))
        return out

    def hints(self) -> list[tuple[str, str]]:
        if self.phase == "running":
            return [("esc", "cancel and roll back")] if not self.cancel.is_set() else [("…", "rolling back")]
        if self.phase == "done":
            return [("↵", "close")]
        hints = super().hints()
        if self.submit_pending:
            hints[0] = ("^↵", "waiting for preview…")
        return hints
