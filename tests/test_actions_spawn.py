"""Spawn: plan + execute with real git (temp repo, local fake origin) and a fake zellij/kitty."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

try:
    from tests.test_actions_jump import DIR, KT, NOW, SOCK, TAKEN, ZJ, R, World, fake_nav, nav_for, session
    from tests._fake_provider import FakeProvider, inject
except ImportError:  # pragma: no cover
    from test_actions_jump import DIR, KT, NOW, SOCK, TAKEN, ZJ, R, World, fake_nav, nav_for, session  # type: ignore
    from _fake_provider import FakeProvider, inject  # type: ignore

from agentree.actions import exec as ex
from agentree.actions import spawn as sp
from agentree.actions.api import AgentChoice, SpawnRequest
from agentree.config import Config
from agentree.providers import registry
from agentree.providers.base import LaunchRequest
from agentree.types import Orphans, SessionNode, Snapshot, Worktree, WorktreeNode, ZPane, ZTab

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main"]


def git(*args, cwd=None):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeLinks:
    """In-memory links.json with the state.py semantics used by the actions."""

    def __init__(self):
        self.by_path = {}
        self.renamed = []
        self.removed = []
        self.fail = False
        self.rename_fail = False

    @property
    def records(self):
        return list(self.by_path.values())

    def swap_link(self, rec):
        if self.fail:
            raise OSError("disk full")
        prev = self.by_path.get(rec["worktreePath"])
        self.by_path[rec["worktreePath"]] = dict(rec)
        return prev

    def swap_link_journaled(self, rec, persist_prev):
        prev = self.by_path.get(rec["worktreePath"])
        persist_prev(prev)                       # raises -> nothing swapped
        if self.fail:
            raise OSError("disk full")
        self.by_path[rec["worktreePath"]] = dict(rec)
        return prev

    def load_links(self):
        return self.records

    def merge_link(self, path, mutate):
        if self.fail:
            raise OSError("disk full")
        cur = self.by_path.get(path)
        new = mutate(dict(cur) if cur is not None else None)
        if new is None:
            self.by_path.pop(path, None)
            return None
        new = dict(new, worktreePath=path)
        new.setdefault("agentPids", [])
        self.by_path[path] = new
        return new

    def restore_link(self, written, previous):
        if self.by_path.get(written["worktreePath"]) != written:
            return False
        if previous is None:
            del self.by_path[written["worktreePath"]]
        else:
            self.by_path[written["worktreePath"]] = previous
        return True

    def remove_link(self, path):
        self.removed.append(path)
        return True

    def rename_link_session(self, old, new):
        if self.rename_fail:
            raise OSError("read-only file system")
        self.renamed.append((old, new))
        return 1


class GitWorld(World):
    """World + real git/cp + fake kill + zellij session lifecycle commands."""

    def __init__(self):
        super().__init__()
        self.killed = []

    def __call__(self, argv, *, env=None, timeout=1.0, log=False, dry_run=False, cwd=None):
        if argv[0] in ("git", "cp"):
            self.calls.append((tuple(argv), dict(env or {}), dry_run))
            return ex.run(argv, env=env, timeout=max(timeout, 10), cwd=cwd, dry_run=dry_run)
        if argv[0] == "kill":
            self.calls.append((tuple(argv), dict(env or {}), dry_run))
            if not dry_run:
                self.killed.append(int(argv[-1]))
            return R(argv, dry=dry_run)
        return super().__call__(argv, env=env, timeout=timeout, log=log, dry_run=dry_run)

    def zellij(self, argv, env):
        d = env.get("ZELLIJ_SOCKET_DIR", DIR)
        if argv[1] == "kill-session":
            s = self.sessions.get((d, argv[2]))
            if s is None or s["exited"]:
                return R(argv, 1, err="not found")
            s["exited"], s["clients"] = True, 0
            return R(argv)
        if argv[1] == "delete-session":
            name = argv[-1]
            if (d, name) not in self.sessions:
                return R(argv, 1, err="not found")
            if not self.sessions[(d, name)]["exited"] and "--force" not in argv:
                return R(argv, 1, err="active")
            del self.sessions[(d, name)]
            return R(argv)
        if argv[1] == "--session" and argv[4] == "close-tab-by-id":
            s = self.sessions[(d, argv[2])]
            tid = int(argv[5])
            s["tabs"] = [t for t in s["tabs"] if t[0] != tid]
            s["panes"] = [p for p in s["panes"] if p[1] != tid]
            return R(argv)
        if argv[1] == "--session" and argv[4] == "rename-session":
            self.sessions[(d, argv[5])] = self.sessions.pop((d, argv[2]))
            return R(argv)
        return super().zellij(argv, env)

    def kitty(self, argv):
        if argv[4] == "launch" and "-s" in argv:
            name = argv[argv.index("-s") + 1]
            d = argv[argv.index("--env") + 1].split("=", 1)[1]
            cwd = argv[argv.index("--cwd") + 1]
            if "-n" in argv:
                lf = argv[argv.index("-n") + 1]
                if os.path.isfile(lf):
                    st = os.stat(lf)
                    with open(lf) as fh:
                        self.layout_seen = (lf, fh.read(), st.st_mode & 0o777)
            self.launched.append(name)
            self.add_session(name, [(0, "Tab #1")], [(0, 0, os.path.realpath(cwd), False)], 1, d=d, age=0)
            var = next((argv[k + 1].split("=", 1)[1] for k, x in enumerate(argv)
                        if x == "--var" and argv[k + 1].startswith("agentree_session=")), None)
            self.window(88, name, title=f"{name} | fish", sock=argv[3],
                        user_vars={"agentree_session": var} if var else None)
            return R(argv, out="88\n")
        if argv[4] == "close-window":
            return R(argv)
        return super().kitty(argv)


class SpawnBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agentree-spawn-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for p in (mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": os.path.join(self.tmp, "state")}),
                  mock.patch.object(sp, "AGENT_WAIT_S", 0.2), mock.patch("agentree.actions.tabs.VERIFY_S", 0.2)):
            p.start()
            self.addCleanup(p.stop)
        self.repo = os.path.realpath(os.path.join(self.tmp, "proj"))
        os.makedirs(self.repo)
        git("init", "-q", cwd=self.repo)
        with open(os.path.join(self.repo, "README"), "w") as fh:
            fh.write("x\n")
        for n, body in ((".env", "A=1\n"), (".env.local", "B=2\n"), (".env.example", "A=\n"),
                        ("package-lock.json", "{}\n")):
            with open(os.path.join(self.repo, n), "w") as fh:
                fh.write(body)
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(".env\n.env.local\nnode_modules/\n")
        git("add", "README", ".gitignore", ".env.example", "package-lock.json", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)
        git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.repo)
        self.base = git("rev-parse", "HEAD", cwd=self.repo)
        sp._default_cache.clear()
        self.oc = _fake_exe(os.path.join(self.tmp, "oc"))          # an available opencode_cmd
        self.cfg = Config(repo_roots=(os.path.join(self.tmp, "*"),),
                          install_commands={"package-lock.json": "npm ci"}, opencode_cmd=self.oc)
        self.w = GitWorld()
        self.w.add_session("S", [(0, "main")], [(0, 0, self.repo, False)], 1)
        self.w.window(5, "S")
        tabs = (ZTab(0, 0, "main", True, (ZPane(0, False, 0, "fish", "fish", ("fish",), self.repo),)),)
        root = Worktree(self.repo, self.repo, "main", self.base, True)
        self.node = SessionNode(session("S", tabs=tabs), (WorktreeNode(root, tabs=tabs),))
        self.snap = Snapshot(TAKEN, (self.node,), Orphans())
        self.links = FakeLinks()
        self.events = []
        self.installs = []
        self.install_rc = 0
        self.install_hook = None

    def stream(self, argv, *, cwd=None, env=None, timeout=600, on_line=None, cancel=None, dry_run=False, log=True):
        self.w.calls.append((tuple(argv), dict(env or {}), dry_run))
        if dry_run:
            return ex.Result(tuple(argv), 0, "", "", False, 0.0, True)
        self.installs.append((tuple(argv), cwd))
        on_line("resolving deps")
        if self.install_hook:
            self.install_hook(cwd, cancel)
        if cancel is not None and cancel.is_set():
            return ex.Result(tuple(argv), None, "", "", False, 0.0, cancelled=True)
        on_line("npm ERR! boom" if self.install_rc else "added 3 packages")
        return ex.Result(tuple(argv), self.install_rc, "npm ERR! boom" if self.install_rc else "ok", "", False, 0.0)

    def nav(self, **kw):
        n = nav_for(self.w, **kw)
        n.runner._stream = self.stream
        return n

    def req(self, **kw):
        base = dict(repo=self.repo, branch="agent/feat-x", base="origin", target="tab", layout=None, prompt="",
                    copy_env=True, run_install=False, switch_after=True)
        base.update(kw)
        return SpawnRequest(**base)

    def plan(self, nav=None, **kw):
        return sp.plan_spawn(self.req(**kw), self.snap, cfg=self.cfg, nav=nav or self.nav(current="OTHER"),
                             state_dir=os.path.join(self.tmp, "state"))

    def run_plan(self, plan, nav=None, cancel=None, **kw):
        nav = nav or self.nav(current="OTHER")
        return sp.execute_spawn(plan, nav, cfg=self.cfg, state_dir=os.path.join(self.tmp, "state"),
                                progress=self.events.append, cancel=cancel, links=self.links,
                                find_agents=kw.get("find_agents", lambda s, ids: []))

    @property
    def wt(self):
        return os.path.join(self.repo, ".worktrees", "feat-x")

    def branch_exists(self, b="agent/feat-x"):
        return subprocess.run(["git", "-C", self.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{b}"]).returncode == 0

    def states(self, kind_index=None):
        return [(e.index, e.state) for e in self.events]


class KdlTest(unittest.TestCase):
    def test_kdl_str(self):
        self.assertEqual(sp.kdl_str('a "b" \\c\nd\te\x01'), '"a \\"b\\" \\\\c\\nd\\te\\u{1}"')
        self.assertEqual(sp.kdl_str(""), '""')

    def test_agent_layout(self):
        # agent_layout only escapes the provider's launch argv into KDL
        lay = sp.agent_layout(("/b/agent", "--prompt", 'say "hi"\nthen \\ go'), pane_name="codex")
        self.assertNotIn("\n", lay)
        self.assertIn('pane size="65%" name="codex" command="/b/agent" focus=true', lay)
        self.assertIn('args "--prompt" "say \\"hi\\"\\nthen \\\\ go"', lay)
        self.assertNotIn("args", sp.agent_layout(("~/oc",), pane_name="x"))
        self.assertIn('command="~/oc"', sp.agent_layout(("~/oc",), pane_name="x"))   # no expansion here

    def test_names(self):
        self.assertEqual(sp.slug_of("agent/feat x!"), "feat-x")
        self.assertEqual(sp.slug_of("feat/"), "feat")
        self.assertEqual(sp.session_name_for("/p/my repo", "x"), "my-repo-x")


class PlanTest(SpawnBase):
    def test_tab_plan(self):
        t0 = time.perf_counter()
        plan = self.plan(run_install=True, prompt='do "it"\nnow', layout="agent")
        ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(plan.errors, ())
        self.assertLess(ms, 200)            # target ~20 ms; generous for CI
        kinds = [s["kind"] for s in plan.data["spec"]]
        self.assertEqual(kinds, ["exclude", "worktree", "env", "install", "tab", "links", "switch"])
        self.assertEqual(plan.steps[1], ("git", "-C", self.repo, "worktree", "add", "-b", "agent/feat-x", self.wt,
                                         "origin/main"))
        self.assertEqual(plan.steps[2], (f"copy .env files (no overwrite, no symlinks): .env, .env.local → {self.wt}/",))
        self.assertEqual(plan.steps[3], (f"cd {self.wt} && npm ci",))
        tab = plan.steps[4]
        self.assertEqual(tab[:8], (ZJ, "--session", "S", "action", "new-tab", "--cwd", self.wt, "--name"))
        self.assertEqual(tab[8:10], ("feat-x", "--layout-string"))
        self.assertIn('args "--prompt" "do \\"it\\"\\nnow"', tab[10])
        self.assertEqual(plan.data["worktree_path"], self.wt)
        self.assertEqual((plan.data["slug"], plan.data["install_cmd"]), ("feat-x", "npm ci"))
        self.assertEqual(plan.warnings, ("install may download packages from the network",))

    def test_plan_is_side_effect_free(self):
        self.plan(run_install=True)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".worktrees")))
        self.assertFalse(self.branch_exists())
        self.assertEqual([c for c in self.w.calls if not c[2] and c[0][0] in (ZJ, KT)
                          and c[0][1] != "list-sessions" and c[0][4] not in ("list-tabs", "list-panes", "list-clients", "ls")], [])

    def test_errors_and_warnings(self):
        self.assertIn("invalid branch name", self.plan(branch="a..b").errors[0])
        self.assertIn("type a branch name", self.plan(branch="  ").errors[0])
        self.assertIn("checked out at", self.plan(branch="main").errors[0])
        self.assertIn("not found", self.plan(repo=os.path.join(self.tmp, "nope")).errors[0])
        os.makedirs(self.wt)
        self.assertIn("already exists", self.plan().errors[0])
        os.rmdir(self.wt)
        git("branch", "agent/feat-x", cwd=self.repo)
        p = self.plan()
        self.assertEqual(p.errors, ())
        self.assertIn("reusing it", p.warnings[0])
        self.assertEqual(p.steps[1], ("git", "-C", self.repo, "worktree", "add", self.wt, "agent/feat-x"))
        git("branch", "-D", "agent/feat-x", cwd=self.repo)
        git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", cwd=self.repo)
        sp._default_cache.clear()
        p = self.plan()
        self.assertIn("branching from HEAD", p.warnings[0])
        self.assertEqual(p.steps[1][-1], "HEAD")
        p = self.plan(copy_env=True, run_install=True, prompt="x", layout="default")
        self.assertIn("built-in agent layout", " ".join(p.warnings))

    def test_no_env_no_lockfile_warnings(self):
        for n in (".env", ".env.local", "package-lock.json"):
            os.unlink(os.path.join(self.repo, n))
        p = self.plan(run_install=True)
        self.assertIn("no .env files", " ".join(p.warnings))
        self.assertIn("no known lockfile", " ".join(p.warnings))
        self.assertIsNone(p.data["install_cmd"])

    def test_session_choice(self):
        snap = Snapshot(TAKEN, (), Orphans())
        p = sp.plan_spawn(self.req(), snap, cfg=self.cfg, nav=self.nav(), state_dir=self.tmp)
        self.assertIn("no live session for proj; choose new session", p.errors)
        node2 = replace(self.node, session=replace(self.node.session, name="T"))
        snap2 = Snapshot(TAKEN, (self.node, node2), Orphans())
        p = sp.plan_spawn(self.req(), snap2, cfg=self.cfg, nav=self.nav(current="X"), state_dir=self.tmp)
        self.assertIn("several sessions (S, T)", p.errors[0])
        p = sp.plan_spawn(self.req(), snap2, cfg=self.cfg, nav=self.nav(current="T"), state_dir=self.tmp)
        self.assertEqual(p.data["session"], "T")
        # a spawned session holds only its worktree: the session with the root checkout open wins
        wt = Worktree(self.repo + "/.worktrees/y", self.repo, "agent/y", self.base, False)
        node3 = replace(self.node, session=replace(self.node.session, name="proj-y"), worktrees=(WorktreeNode(wt),))
        p = sp.plan_spawn(self.req(), Snapshot(TAKEN, (node3, self.node), Orphans()), cfg=self.cfg,
                          nav=self.nav(current="X"), state_dir=self.tmp)
        self.assertEqual(p.data["session"], "S")
        bad = replace(self.node, session=replace(self.node.session, inventory_complete=False))
        p = sp.plan_spawn(self.req(), Snapshot(TAKEN, (bad,), Orphans()), cfg=self.cfg, nav=self.nav(),
                          state_dir=self.tmp)
        self.assertIn("inventory incomplete", p.errors[0])

    def test_session_plan(self):
        p = self.plan(target="session", prompt="hi", switch_after=False)
        self.assertEqual(p.errors, ())
        self.assertEqual(p.data["session"], "proj-feat-x")
        argv = p.steps[[s["kind"] for s in p.data["spec"]].index("session")]
        lf = p.data["layout_file"]
        self.assertEqual(argv, (KT, "@", "--to", SOCK, "launch", "--type=tab", "--cwd", self.wt, "--tab-title",
                                "proj-feat-x", "--var", "agentree_session=proj-feat-x", "--env",
                                f"ZELLIJ_SOCKET_DIR={DIR}", "--keep-focus", ZJ, "-s", "proj-feat-x", "-n", lf))
        node = replace(self.node, session=replace(self.node.session, name="proj-feat-x"))
        p = sp.plan_spawn(self.req(target="session"), Snapshot(TAKEN, (self.node, node), Orphans()),
                          cfg=self.cfg, nav=self.nav(), state_dir=self.tmp)
        self.assertIn("session proj-feat-x already exists", p.errors)


class ExecuteTest(SpawnBase):
    def test_success_tab(self):
        plan = self.plan(run_install=True, prompt='go "now"')
        res = self.run_plan(plan)
        self.assertTrue(res.ok, res.message)
        self.assertTrue(res.close and res.refresh)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.branch_exists())
        self.assertEqual(sorted(n for n in os.listdir(self.wt) if n.startswith(".env")),
                         [".env", ".env.example", ".env.local"])     # .example is tracked
        with open(os.path.join(self.repo, ".git", "info", "exclude")) as fh:
            self.assertIn(".worktrees/", fh.read().splitlines())
        self.assertEqual(self.installs, [(("npm", "ci"), self.wt)])
        new = [a for a in self.w.acts() if a[:3] == ("zj", "S", "new-tab")]
        self.assertEqual(len(new), 1)
        self.assertIn("--layout-string", new[0])
        self.assertEqual(self.links.records, [{"worktreePath": self.wt, "session": "S", "tabName": "feat-x",
                                               "tabId": 1, "agentPids": []}])
        self.assertIn(("zj", "S", "go-to-tab-by-id", "1"), self.w.acts())
        self.assertIn(("kitty", SOCK, "focus-window", "--match", "id:5"), self.w.acts())
        states = [e.state for e in self.events]
        self.assertEqual(states.count("ok"), len(plan.steps))
        self.assertIn(("running", "resolving deps"), [(e.state, e.detail) for e in self.events if e.index == 3])

    def test_agent_pids_recorded(self):
        plan = self.plan(prompt="x")
        seen = {}

        def find(session, ids):
            seen["args"] = (session, ids)
            return [4242]

        res = self.run_plan(plan, find_agents=find)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.links.records[0]["agentPids"], [4242])
        self.assertEqual(seen["args"], ("S", {101}))

    def test_install_failure_rolls_back(self):
        self.install_rc = 1
        plan = self.plan(run_install=True)
        res = self.run_plan(plan)
        self.assertFalse(res.ok)
        self.assertIn("install failed", res.message)
        self.assertIn("rolled back: worktree, branch agent/feat-x", res.message)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        self.assertFalse([a for a in self.w.acts() if a[:3] == ("zj", "S", "new-tab")])
        by = {(e.index, e.state) for e in self.events}
        self.assertIn((3, "failed"), by)
        self.assertIn((4, "skipped"), by)
        self.assertIn((1, "rolled_back"), by)
        self.assertEqual(self.links.records, [])

    def test_failure_after_tab_closes_it(self):
        self.links.fail = True
        plan = self.plan()
        res = self.run_plan(plan)
        self.assertFalse(res.ok)
        self.assertIn("tab 1", res.message)
        self.assertIn(("zj", "S", "close-tab-by-id", "1"), self.w.acts())
        self.assertFalse(os.path.exists(self.wt))
        self.assertEqual([t for t, _ in self.w.s("S")["tabs"]], [0])

    def test_unverified_tab_is_not_closed(self):
        self.w.new_tab_name = "something-else"
        res = self.run_plan(self.plan())
        self.assertFalse(res.ok)
        self.assertNotIn("close-tab-by-id", [a[2] for a in self.w.acts() if a[0] == "zj"])
        self.assertIn("kept: tab 1 in S: now named 'something-else'", res.message)
        self.assertFalse(os.path.exists(self.wt))

    def test_not_pristine_worktree_is_kept(self):
        def commit_then_fail(cwd, cancel):
            with open(os.path.join(cwd, "new.txt"), "w") as fh:
                fh.write("work\n")
        self.install_hook = commit_then_fail
        self.install_rc = 2
        res = self.run_plan(self.plan(run_install=True))
        self.assertFalse(res.ok)
        self.assertIn(f"kept {self.wt}: contains files not created by agentree", res.message)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.branch_exists())

    def test_moved_branch_is_kept(self):
        def commit(cwd, cancel):
            git("commit", "-q", "--allow-empty", "-m", "x", cwd=cwd)
        self.install_hook = commit
        self.install_rc = 2
        res = self.run_plan(self.plan(run_install=True))
        self.assertIn("HEAD moved", res.message)
        self.assertTrue(self.branch_exists())

    def test_cancel_during_install(self):
        cancel = threading.Event()
        self.install_hook = lambda cwd, c: cancel.set()
        res = self.run_plan(self.plan(run_install=True), cancel=cancel)
        self.assertFalse(res.ok)
        self.assertIn("cancelled", res.message)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())

    def test_cancel_before_start(self):
        cancel = threading.Event()
        cancel.set()
        res = self.run_plan(self.plan(), cancel=cancel)
        self.assertFalse(res.ok)
        self.assertIn("nothing to roll back", res.message)
        self.assertFalse(os.path.exists(self.wt))

    def test_reused_branch_not_deleted_on_rollback(self):
        git("branch", "agent/feat-x", cwd=self.repo)
        self.install_rc = 1
        res = self.run_plan(self.plan(run_install=True))
        self.assertFalse(res.ok)
        self.assertFalse(os.path.exists(self.wt))
        self.assertTrue(self.branch_exists())

    def test_preconditions_rechecked(self):
        plan = self.plan()
        os.makedirs(self.wt)
        res = self.run_plan(plan)
        self.assertIn("appeared since the preview", res.message)
        self.assertTrue(os.path.isdir(self.wt))           # not ours: never removed

    def test_session_target(self):
        plan = self.plan(target="session", prompt='hi "x"')
        res = self.run_plan(plan)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.w.launched, ["proj-feat-x"])
        lf, content, mode = self.w.layout_seen
        self.assertEqual(lf, plan.data["layout_file"])
        self.assertIn('args "--prompt" "hi \\"x\\""', content)
        self.assertEqual(mode, 0o600)
        self.assertFalse(os.path.exists(lf))                      # removed once the session runs
        self.assertEqual(self.links.records[0]["tabName"], "Tab #1")   # the real first tab
        self.assertEqual(self.links.records[0]["tabId"], 0)
        self.assertEqual(self.links.records[0]["session"], "proj-feat-x")
        self.assertIn(("kitty", SOCK, "focus-window", "--match", "id:88"), self.w.acts())

    def test_session_target_rollback_kills_session(self):
        self.links.fail = True
        res = self.run_plan(self.plan(target="session"))
        self.assertFalse(res.ok)
        self.assertIn("session proj-feat-x", res.message)
        self.assertNotIn((DIR, "proj-feat-x"), self.w.sessions)
        self.assertFalse(os.path.exists(self.wt))

    def test_session_appearing_meanwhile_refused(self):
        plan = self.plan(target="session")
        self.w.add_session("proj-feat-x", [(0, "t")], [], 1)
        res = self.run_plan(plan)
        self.assertIn("already exists", res.message)
        self.assertIn((DIR, "proj-feat-x"), self.w.sessions)     # not ours: untouched

    def test_dry_run(self):
        plan = self.plan(run_install=True)
        nav = self.nav(current="OTHER", dry_run=True)
        res = self.run_plan(plan, nav=nav)
        self.assertTrue(res.ok, res.message)
        self.assertFalse(res.close)
        self.assertRegex(res.message, r"^dry-run: \d+ commands logged$")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".worktrees")))
        self.assertFalse(self.branch_exists())
        self.assertEqual(self.installs, [])
        self.assertEqual(self.links.records, [])
        self.assertEqual(len(self.w.s("S")["tabs"]), 1)
        with open(os.path.join(self.tmp, "state", "log")) as fh:
            log = fh.read()
        self.assertIn("DRY", log)
        self.assertIn("worktree add -b agent/feat-x", log)


class OptionsTest(SpawnBase):
    def test_options(self):
        from agentree.types import Row
        r = Row(key="k", kind="worktree", depth=0, caret=None, icon=None, label=(), meta=(),
                ref=self.node.worktrees[0], session="S")
        o = sp.spawn_options(r, self.snap, self.cfg)
        self.assertEqual([c.id for c in o.agents], list(registry.ids()))
        self.assertTrue(o.agents[0].available)                          # opencode_cmd is executable
        self.assertEqual(o.default_provider, "opencode")
        self.assertIn(self.repo, o.repos)
        self.assertEqual(o.default_repo, self.repo)
        self.assertEqual(o.layouts, ("agent", "default"))
        r2 = replace(r, kind="session", ref=self.node)
        self.assertEqual(sp.spawn_options(r2, self.snap, self.cfg).default_repo, self.repo)
        self.assertIsNone(sp.spawn_options(None, self.snap, self.cfg).default_repo)


if __name__ == "__main__":
    unittest.main()


class CliSpawnTest(SpawnBase):
    def test_cli_spawn_dry_run_and_errors(self):
        import argparse
        import io

        from agentree import cli
        from agentree.actions.real import RealActions

        a = RealActions(self.cfg, dry_run=True, run_fn=self.w, environ={"ZELLIJ_SOCKET_DIR": DIR},
                        sockets=lambda: [SOCK], stream_fn=self.stream, links=self.links,
                        nav_overrides=fake_nav(self.w, clock=lambda: NOW, owned=lambda s: True,
                                                socket_dirs=lambda: [DIR], zellij=ZJ, kitty=KT))
        ns = argparse.Namespace(repo="proj", branch="agent/feat-x", base="origin", session=False, layout=None,
                                prompt="", no_env=False, install=True, no_switch=False)
        out = io.StringIO()
        with mock.patch("agentree.config.load_config", return_value=self.cfg):
            code = cli.cmd_spawn(ns, dry_run=True, actions=a, snapshot=self.snap, out=out)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn(f"$ git -C {self.repo} worktree add -b agent/feat-x {self.wt} origin/main", text)
        self.assertIn("✓ dry-run:", text)
        self.assertFalse(os.path.exists(self.wt))
        out = io.StringIO()
        ns.branch = "bad..name"
        with mock.patch("agentree.config.load_config", return_value=self.cfg):
            self.assertEqual(cli.cmd_spawn(ns, dry_run=True, actions=a, snapshot=self.snap, out=out), 1)
        self.assertIn("✗ invalid branch name", out.getvalue())
        ns.repo = "nope"
        with mock.patch("agentree.config.load_config", return_value=self.cfg):
            self.assertEqual(cli.cmd_spawn(ns, dry_run=True, actions=a, snapshot=self.snap, out=io.StringIO()), 2)

    def test_parser(self):
        from agentree import cli
        a = cli._parser().parse_args(["spawn", "proj", "agent/x", "--session", "--prompt", "hi", "--install",
                                      "--no-switch", "--no-env", "--base", "head", "--layout", "agent", "--dry-run"])
        self.assertEqual((a.cmd, a.repo, a.branch, a.session, a.prompt, a.install, a.no_switch, a.no_env, a.base,
                          a.layout, a.spawn_dry_run), ("spawn", "proj", "agent/x", True, "hi", True, True, True,
                                                       "head", "agent", True))


class PristineTest(SpawnBase):
    """Finding 1: only files agentree created may be deleted by a rollback."""

    def fail_install_with(self, hook):
        self.install_hook = hook
        self.install_rc = 1
        return self.run_plan(self.plan(run_install=True))

    def test_ignored_files_keep_the_worktree(self):
        def make_node_modules(cwd, cancel):
            os.makedirs(os.path.join(cwd, "node_modules", "x"))
            with open(os.path.join(cwd, "node_modules", "x", "i.js"), "w") as fh:
                fh.write("1\n")
        res = self.fail_install_with(make_node_modules)
        self.assertIn(f"kept {self.wt}: contains files not created by agentree", res.message)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.branch_exists())
        wt_events = [(e.state, e.detail) for e in self.events if e.index == 1]
        self.assertIn(("failed", f"kept {self.wt}: contains files not created by agentree"), wt_events)
        self.assertNotIn("rolled_back", [s for s, _ in wt_events])

    def test_modified_copied_env_keeps_the_worktree(self):
        def edit_env(cwd, cancel):
            with open(os.path.join(cwd, ".env"), "a") as fh:
                fh.write("EDITED=1\n")
        res = self.fail_install_with(edit_env)
        self.assertIn("contains files not created by agentree", res.message)
        self.assertTrue(os.path.isdir(self.wt))

    def test_replaced_copied_env_by_symlink_keeps_the_worktree(self):
        def swap(cwd, cancel):
            os.unlink(os.path.join(cwd, ".env"))
            os.symlink(os.path.join(self.repo, ".env"), os.path.join(cwd, ".env"))
        res = self.fail_install_with(swap)
        self.assertIn("contains files not created by agentree", res.message)

    def test_tracked_change_keeps_the_worktree(self):
        def edit(cwd, cancel):
            with open(os.path.join(cwd, "README"), "a") as fh:
                fh.write("more\n")
        res = self.fail_install_with(edit)
        self.assertIn("tracked files changed", res.message)

    def test_unchanged_copies_are_removed(self):
        res = self.fail_install_with(None)
        self.assertIn("rolled back: worktree, branch agent/feat-x", res.message)
        self.assertFalse(os.path.exists(self.wt))

    def test_pristine_unit(self):
        from agentree.actions.runner import Runner
        r = Runner(run_fn=lambda argv, **kw: ex.run(argv, **{k: v for k, v in kw.items() if k != "log"}))
        git("worktree", "add", "-q", "-b", "agent/u", self.wt, "HEAD", cwd=self.repo)
        head = git("rev-parse", "HEAD", cwd=self.wt)
        base = sp.baseline(r, self.wt)
        self.assertEqual(base, {})
        copied, skipped = sp.copy_env_files(self.repo, self.wt, [".env"])
        self.assertEqual(sp.pristine(r, self.wt, head, base, copied), (True, ""))
        self.assertEqual(sp.pristine(r, self.wt, head, None, copied)[0], False)        # baseline unknown
        self.assertEqual(sp.pristine(r, self.wt, "0" * 40, base, copied), (False, "HEAD moved"))


class EnvCopyTest(SpawnBase):
    """Finding 4: in-process copy, no overwrite, no symlinks, 0600, mtime kept."""

    def test_copy_semantics(self):
        os.makedirs(self.wt)
        os.utime(os.path.join(self.repo, ".env"), (1_600_000_000, 1_600_000_000))
        target = os.path.join(self.tmp, "victim")
        with open(target, "w") as fh:
            fh.write("do not touch\n")
        os.symlink(target, os.path.join(self.wt, ".env.local"))              # dst is a symlink
        os.symlink(os.path.join(self.repo, ".env"), os.path.join(self.repo, ".env.link"))  # src symlink
        copied, skipped = sp.copy_env_files(self.repo, self.wt, [".env", ".env.local", ".env.link", ".env.gone"])
        self.assertEqual(list(copied), [".env"])
        st = os.lstat(os.path.join(self.wt, ".env"))
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertEqual(int(st.st_mtime), 1_600_000_000)
        with open(target) as fh:
            self.assertEqual(fh.read(), "do not touch\n")
        self.assertEqual(len(skipped), 3)
        self.assertTrue(any(".env.local: already in the worktree" in x for x in skipped))
        self.assertTrue(any(".env.link: not a regular file" in x for x in skipped))
        import hashlib
        self.assertEqual(copied[".env"], hashlib.sha256(b"A=1\n").hexdigest())

    def test_tracked_env_predicted_and_not_overwritten(self):
        with open(os.path.join(self.repo, ".env.local"), "w") as fh:
            fh.write("TRACKED=1\n")
        git("add", "-f", ".env.local", cwd=self.repo)
        git("commit", "-q", "-m", "track env", cwd=self.repo)
        git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)
        with open(os.path.join(self.repo, ".env.local"), "w") as fh:
            fh.write("LOCAL=secret\n")
        plan = self.plan()
        self.assertIn(".env.local is tracked in origin/main: not copied (never overwritten)", plan.warnings)
        self.assertEqual(plan.steps[2], (f"copy .env files (no overwrite, no symlinks): .env → {self.wt}/",))
        res = self.run_plan(plan)
        self.assertTrue(res.ok, res.message)
        with open(os.path.join(self.wt, ".env.local")) as fh:
            self.assertEqual(fh.read(), "TRACKED=1\n")

    def test_no_cp_subprocess(self):
        self.run_plan(self.plan())
        self.assertFalse([c for c in self.w.calls if c[0][0] == "cp"])


class JournalLockTest(SpawnBase):
    """Finding 5: locks, journal, failed `worktree add`, identity before rollback."""

    def test_journal_persisted_during_run_and_removed(self):
        from agentree import state
        seen = []
        self.install_hook = lambda cwd, c: seen.append(state.list_journals())
        res = self.run_plan(self.plan(run_install=True))
        self.assertTrue(res.ok, res.message)
        self.assertEqual(len(seen[0]), 1)
        kinds = [e["kind"] for e in seen[0][0]["data"]["entries"]]
        self.assertEqual(kinds, ["branch", "worktree", "env"])
        self.assertEqual(seen[0][0]["data"]["entries"][1]["state"], "created")
        self.assertEqual(state.list_journals(), [])

    def test_journal_kept_when_something_is_kept(self):
        from agentree import state

        def junk(cwd, c):
            with open(os.path.join(cwd, "junk"), "w") as fh:
                fh.write("x")
        self.install_hook = junk
        self.install_rc = 1
        self.run_plan(self.plan(run_install=True))
        left = state.list_journals()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["data"]["worktree"], self.wt)

    def test_repo_lock(self):
        from agentree.actions.tabs import repo_lock_path
        import fcntl
        path = repo_lock_path(os.path.join(self.tmp, "state"), self.repo)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh, mock.patch("agentree.actions.tabs.LOCK_WAIT_S", 0.1):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            res = sp.execute_spawn(self.plan(), self.nav(current="OTHER"), cfg=self.cfg,
                                   state_dir=os.path.join(self.tmp, "state"), links=self.links)
        self.assertFalse(res.ok)
        self.assertIn("another agentree is changing this repo", res.message)
        self.assertFalse(os.path.exists(self.wt))

    def test_session_name_lock(self):
        from agentree.actions.tabs import session_name_lock_path
        import fcntl
        path = session_name_lock_path(os.path.join(self.tmp, "state"), "proj-feat-x")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh, mock.patch("agentree.actions.tabs.LOCK_WAIT_S", 0.1):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            res = self.run_plan(self.plan(target="session"))
        self.assertIn("another agentree is creating session proj-feat-x", res.message)
        self.assertEqual(self.w.launched, [])
        self.assertFalse(os.path.exists(self.wt))                  # rolled back

    def test_failed_worktree_add_that_created_things(self):
        orig = GitWorld.__call__

        def half(world, argv, **kw):
            r = orig(world, argv, **kw)
            if argv[0] == "git" and "worktree" in argv and "add" in argv:
                return R(argv, 128, err="fatal: killed")             # it did create both
            return r
        with mock.patch.object(GitWorld, "__call__", half):
            res = self.run_plan(self.plan())
        self.assertIn("worktree add: fatal: killed", res.message)
        self.assertIn("rolled back: worktree, branch agent/feat-x", res.message)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())

    def test_failed_worktree_add_with_foreign_branch(self):
        orig = GitWorld.__call__

        def racing(world, argv, **kw):
            if argv[0] == "git" and "worktree" in argv and "add" in argv:
                git("branch", "agent/feat-x", "HEAD~0", cwd=self.repo)
                git("commit", "-q", "--allow-empty", "-m", "someone else", cwd=self.repo)
                git("branch", "-f", "agent/feat-x", "HEAD", cwd=self.repo)
                return R(argv, 128, err="fatal: a branch named 'agent/feat-x' already exists")
            return orig(world, argv, **kw)
        with mock.patch.object(GitWorld, "__call__", racing):
            res = self.run_plan(self.plan())
        self.assertIn("nothing to roll back", res.message)
        self.assertTrue(self.branch_exists())                      # not ours: kept

    def test_tab_id_already_gone_counts_as_resolved(self):
        from agentree import state
        self.w.new_tab_out = "1\n"            # printed an id but created nothing with that id
        res = self.run_plan(self.plan())
        self.assertFalse(res.ok)
        self.assertNotIn("kept", res.message)
        self.assertIn("rolled back: worktree, branch agent/feat-x", res.message)
        self.assertEqual(state.list_journals(), [])

    def test_session_recreated_before_rollback_is_kept(self):
        world = self.w

        def recreated_then_fail(rec, persist_prev):
            world.s("proj-feat-x")["age"] = 500     # someone killed + re-created it long ago?!
            raise OSError("disk full")
        self.links.swap_link_journaled = recreated_then_fail
        res = self.run_plan(self.plan(target="session"))
        self.assertFalse(res.ok)
        self.assertIn("kept: session proj-feat-x: not the incarnation we created", res.message)
        self.assertFalse(world.s("proj-feat-x")["exited"])
        self.assertFalse(os.path.exists(self.wt))               # pristine: rolled back
        self.assertNotIn("links.json record", res.message)       # the failed write wrote nothing

    def test_session_incarnation_mismatch_is_kept(self):
        e = {"kind": "session", "name": "S", "socket_dir": DIR, "age": 0, "seen": time.time() - 1000}
        self.assertFalse(sp._age_ok(e, 2))
        self.assertTrue(sp._age_ok(dict(e, seen=time.time()), 1))


class OwnTabTest(SpawnBase):
    """Finding 3 (spawn side): a rollback never closes the tab agentree runs in."""

    def test_rollback_does_not_close_own_tab(self):
        self.links.fail = True
        nav = self.nav(current="S")
        nav.env = replace(nav.env, pane_id="101")       # agentree's pane = the new tab's pane (contrived)
        res = self.run_plan(self.plan(), nav=nav)
        self.assertIn("agentree itself runs in it", res.message)
        self.assertNotIn("close-tab-by-id", [a[2] for a in self.w.acts() if a[0] == "zj"])


class LinksRollbackTest(SpawnBase):
    """Finding 8: the previous record is restored; a record changed by someone else is kept."""

    def test_previous_record_restored(self):
        old = {"worktreePath": self.wt, "session": "OLD", "tabName": "t", "agentPids": [1]}
        self.links.by_path[self.wt] = dict(old)
        orig_switch = None
        plan = self.plan()
        nav = self.nav(current="OTHER")
        with mock.patch.object(nav, "go", side_effect=_Boom):
            res = self.run_plan(plan, nav=nav)
        self.assertFalse(res.ok)
        self.assertEqual(self.links.by_path[self.wt], old)
        self.assertIsNone(orig_switch)

    def test_record_changed_meanwhile_is_kept(self):
        plan = self.plan()
        nav = self.nav(current="OTHER")

        def other_writer(*a, **k):
            self.links.by_path[self.wt] = {"worktreePath": self.wt, "session": "X", "tabName": "y", "agentPids": []}
            raise _Boom("switch failed hard")
        with mock.patch.object(nav, "go", side_effect=other_writer):
            res = self.run_plan(plan, nav=nav)
        self.assertIn("links.json record", res.message)
        self.assertEqual(self.links.by_path[self.wt]["session"], "X")

    def test_state_helpers(self):
        from agentree import state
        rec = {"worktreePath": "/w", "session": "S", "tabName": "t", "agentPids": []}
        self.assertIsNone(state.swap_link(rec))
        rec2 = dict(rec, session="T")
        self.assertEqual(state.swap_link(rec2)["session"], "S")
        self.assertTrue(state.restore_link(rec2, rec))
        self.assertEqual(state.load_links()[0]["session"], "S")
        self.assertFalse(state.restore_link(rec2, None))          # current is rec, not rec2
        self.assertTrue(state.restore_link(rec, None))
        self.assertEqual(state.load_links(), [])


class _Boom(OSError):
    pass


# =========================================================================== gate M3 attempt 2

class FlakyStore:
    """Journal store: records every write (deep copy) in order; `fail_when(data)` -> raise OSError."""

    def __init__(self, log=None, fail_when=None):
        self.writes = []
        self.deleted = []
        self.log = log if log is not None else []
        self.fail_when = fail_when or (lambda data: False)

    def write_journal(self, run_id, data):
        import copy
        if self.fail_when(data):
            self.log.append(("journal-fail", None))
            raise OSError("No space left on device")
        snap = copy.deepcopy(data)
        self.writes.append(snap)
        self.log.append(("journal", snap))

    def delete_journal(self, run_id):
        self.deleted.append(run_id)


class DirFingerprintTest(SpawnBase):
    """Finding 1: directories in the baseline are fingerprinted recursively."""

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.repo, ".gitignore"), "a") as fh:
            fh.write("cache/\n")
        git("add", ".gitignore", cwd=self.repo)
        git("commit", "-q", "-m", "ignore cache", cwd=self.repo)
        git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)
        hook = os.path.join(self.repo, ".git", "hooks", "post-checkout")
        with open(hook, "w") as fh:     # a checkout hook leaves a nested repo in an ignored dir
            fh.write("#!/bin/sh\nunset GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE GIT_PREFIX\n"
                     "mkdir -p cache/inner && git init -q cache/inner && echo v1 > cache/inner/f\n")
        os.chmod(hook, 0o755)

    def run_failing(self, hook):
        self.install_hook = hook
        self.install_rc = 1
        return self.run_plan(self.plan(run_install=True))

    def test_baseline_dir_recorded(self):
        from agentree import state
        seen = []
        self.install_hook = lambda cwd, c: seen.append(state.list_journals())
        self.run_plan(self.plan(run_install=True))
        wt = [e for e in seen[0][0]["data"]["entries"] if e["kind"] == "worktree"][0]
        self.assertIn("cache/inner/", wt["baseline"])
        self.assertTrue(wt["baseline"]["cache/inner/"].startswith("dir:"))

    def test_unchanged_baseline_dir_is_removed(self):
        res = self.run_failing(None)
        self.assertIn("rolled back: worktree", res.message)
        self.assertFalse(os.path.exists(self.wt))

    def test_new_file_in_baseline_dir_keeps(self):
        def add(cwd, c):
            with open(os.path.join(cwd, "cache", "inner", "new.txt"), "w") as fh:
                fh.write("mine\n")
        res = self.run_failing(add)
        self.assertIn(f"kept {self.wt}: contains files not created by agentree", res.message)
        self.assertTrue(os.path.isfile(os.path.join(self.wt, "cache", "inner", "new.txt")))

    def test_changed_file_in_baseline_dir_keeps(self):
        def change(cwd, c):
            p = os.path.join(cwd, "cache", "inner", "f")
            st = os.stat(p)
            with open(p, "w") as fh:
                fh.write("v2\n")                     # same size, different bytes
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))   # even with the old mtime
        res = self.run_failing(change)
        self.assertIn("contains files not created by agentree", res.message)
        self.assertTrue(os.path.isdir(self.wt))

    def test_fingerprint_unit(self):
        d = os.path.join(self.tmp, "fp")
        os.makedirs(os.path.join(d, "a"))
        with open(os.path.join(d, "a", "x"), "w") as fh:
            fh.write("1")
        os.symlink("/etc/hosts", os.path.join(d, "link"))     # never followed
        fp1 = sp.fingerprint(d)
        self.assertTrue(fp1.startswith("dir:"))
        self.assertEqual(sp.fingerprint(d + "/"), fp1)
        with mock.patch.object(sp, "FP_MAX_ENTRIES", 2):
            self.assertIsNone(sp.fingerprint(d))                  # cap exceeded -> unknown
        os.mkfifo(os.path.join(d, "a", "pipe"))
        self.assertIsNone(sp.fingerprint(d))                      # other type -> unknown
        self.assertIsNone(sp.fingerprint(os.path.join(d, "a", "pipe")))
        self.assertIsNone(sp.fingerprint(os.path.join(d, "missing")))

    def test_unknown_never_matches_unknown(self):
        from agentree.actions.runner import Runner
        r = Runner(run_fn=lambda argv, **kw: ex.run(argv, **{k: v for k, v in kw.items() if k != "log"}))
        git("worktree", "add", "-q", "-b", "agent/u", self.wt, "HEAD", cwd=self.repo)
        head = git("rev-parse", "HEAD", cwd=self.wt)
        os.mkfifo(os.path.join(self.wt, "cache", "inner", "pipe"))
        base = sp.baseline(r, self.wt)
        self.assertIsNone(base["cache/inner/"])
        self.assertEqual(sp.pristine(r, self.wt, head, base, {}), (False, "contains files not created by agentree"))


class JournalDurabilityTest(SpawnBase):
    """Finding 3a + 3c: journal writes gate creation; the replaced link record is durable first."""

    def test_journal_unwritable_creates_nothing(self):
        store = FlakyStore(fail_when=lambda data: True)
        res = sp.execute_spawn(self.plan(), self.nav(current="OTHER"), cfg=self.cfg,
                               state_dir=os.path.join(self.tmp, "state"), links=self.links, journal_store=store)
        self.assertFalse(res.ok)
        self.assertIn("journal not writable", res.message)
        self.assertFalse(os.path.exists(self.wt))
        self.assertFalse(self.branch_exists())
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "info", "exclude.agentree")))
        self.assertEqual([a for a in self.w.acts() if a[0] == "zj"], [])

    def test_journal_fails_before_tab_creation(self):
        store = FlakyStore(fail_when=lambda data: any(e["kind"] == "tab" for e in data["entries"]))
        res = sp.execute_spawn(self.plan(), self.nav(current="OTHER"), cfg=self.cfg,
                               state_dir=os.path.join(self.tmp, "state"), links=self.links, journal_store=store,
                               progress=self.events.append)
        self.assertFalse(res.ok)
        self.assertIn("journal not writable", res.message)
        self.assertEqual([a for a in self.w.acts() if a[:3] == ("zj", "S", "new-tab")], [])   # never created
        self.assertFalse(os.path.exists(self.wt))                                            # rolled back
        self.assertFalse(self.branch_exists())
        self.assertNotIn("uncertain", res.message)

    def test_previous_link_persisted_before_swap(self):
        old = {"worktreePath": self.wt, "session": "OLD", "tabName": "t", "agentPids": [7]}
        self.links.by_path[self.wt] = dict(old)
        order = []
        store = FlakyStore(log=order)
        orig = self.links.swap_link_journaled

        def spy(rec, persist_prev):
            def p(prev):
                persist_prev(prev)
                order.append(("swap", None))
            return orig(rec, p)
        self.links.swap_link_journaled = spy
        res = sp.execute_spawn(self.plan(), self.nav(current="OTHER"), cfg=self.cfg,
                               state_dir=os.path.join(self.tmp, "state"), links=self.links, journal_store=store)
        self.assertTrue(res.ok, res.message)
        i = order.index(("swap", None))
        last = order[i - 1][1]
        link = [e for e in last["entries"] if e["kind"] == "link"][0]
        self.assertEqual((link["state"], link["previous"]), ("swapping", old))

    def test_crash_between_swap_and_journal_update_is_recoverable(self):
        old = {"worktreePath": self.wt, "session": "OLD", "tabName": "t", "agentPids": []}
        self.links.by_path[self.wt] = dict(old)
        # every journal write after the swap fails (process "dies" for the journal)
        dead = {"v": False}

        def after_swap(data):
            dead["v"] = dead["v"] or any(e["kind"] == "link" and e["state"] == "created" for e in data["entries"])
            return dead["v"]
        store = FlakyStore(fail_when=after_swap)
        res = sp.execute_spawn(self.plan(), self.nav(current="OTHER"), cfg=self.cfg,
                               state_dir=os.path.join(self.tmp, "state"), links=self.links, journal_store=store)
        self.assertFalse(res.ok)
        # the in-process rollback restored it ...
        self.assertEqual(self.links.by_path[self.wt], old)
        # ... and the on-disk journal alone would have been enough
        link = [e for e in store.writes[-1]["entries"] if e["kind"] == "link"][0]
        self.assertEqual(link["state"], "swapping")
        self.assertEqual(link["previous"], old)

    def test_crash_recovery_with_real_state(self):
        from agentree import state
        old = {"worktreePath": "/w", "session": "OLD", "tabName": "t", "agentPids": []}
        state.swap_link(old)
        new = {"worktreePath": "/w", "session": "NEW", "tabName": "x", "agentPids": []}

        def persist(prev):
            state.write_journal("run1", {"entries": [{"kind": "link", "state": "swapping", "written": new,
                                                      "previous": prev}]})
        state.swap_link_journaled(new, persist)
        # crash here: no further journal update. Recovery from disk:
        j = state.list_journals()[0]["data"]["entries"][0]
        self.assertTrue(state.restore_link(j["written"], j["previous"]))
        self.assertEqual(state.load_links()[0]["session"], "OLD")

    def test_persist_failure_means_no_swap(self):
        from agentree import state
        old = {"worktreePath": "/w", "session": "OLD", "tabName": "t", "agentPids": []}
        state.swap_link(old)

        def boom(prev):
            raise OSError("journal gone")
        with self.assertRaises(OSError):
            state.swap_link_journaled({"worktreePath": "/w", "session": "NEW", "tabName": "x"}, boom)
        self.assertEqual(state.load_links()[0]["session"], "OLD")


class UnresolvedCleanupTest(SpawnBase):
    """Finding 3b: pending or failed cleanups stay unresolved; the journal is kept."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(sp, "SESSION_WAIT_S", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def launch_without_session(self):
        orig = GitWorld.kitty

        def kitty(world, argv):
            if argv[4] == "launch" and "-s" in argv:
                world.launched.append(argv[argv.index("-s") + 1])
                return R(argv, out="88\n")              # window opened, zellij never came up
            return orig(world, argv)
        return mock.patch.object(GitWorld, "kitty", kitty)

    def test_pending_session_never_seen_stays_uncertain(self):
        from agentree import state
        with self.launch_without_session():
            res = self.run_plan(self.plan(target="session"))
        self.assertFalse(res.ok)
        self.assertIn("uncertain: session proj-feat-x: launched but never seen", res.message)
        left = state.list_journals()
        self.assertEqual(len(left), 1)
        sess = [e for e in left[0]["data"]["entries"] if e["kind"] == "session"][0]
        self.assertEqual(sess["state"], "pending")
        self.assertIn("launched but never seen", sess["unresolved"])
        details = [e.detail for e in self.events if e.state == "failed"]
        self.assertTrue(any(d.startswith("uncertain:") for d in details))

    def test_pending_session_appearing_late_is_rolled_back(self):
        from agentree import state
        orig_kitty, orig_zellij = GitWorld.kitty, GitWorld.zellij
        hidden = {"n": 0}

        def kitty(world, argv):
            r = orig_kitty(world, argv)
            if argv[4] == "launch" and "-s" in argv:
                hidden["n"] = 1                          # the first listing does not show it yet
            return r

        def zellij(world, argv, env):
            if argv[1] == "list-sessions" and hidden["n"] > 0:
                hidden["n"] -= 1
                saved = world.sessions.pop((DIR, "proj-feat-x"))
                try:
                    return orig_zellij(world, argv, env)
                finally:
                    world.sessions[(DIR, "proj-feat-x")] = saved
            return orig_zellij(world, argv, env)
        with mock.patch.object(GitWorld, "kitty", kitty), mock.patch.object(GitWorld, "zellij", zellij):
            res = self.run_plan(self.plan(target="session"))
        self.assertFalse(res.ok)
        self.assertIn("session proj-feat-x", res.message.split("rolled back:")[1])
        self.assertNotIn((DIR, "proj-feat-x"), self.w.sessions)
        self.assertEqual(state.list_journals(), [])

    def test_kill_failure_is_kept(self):
        from agentree import state
        self.links.fail = True
        orig = GitWorld.zellij

        def no_kill(world, argv, env):
            if argv[1] in ("kill-session", "delete-session"):
                return R(argv, 1, err="permission denied")
            return orig(world, argv, env)
        with mock.patch.object(GitWorld, "zellij", no_kill), mock.patch.object(sp, "SESSION_WAIT_S", 2.0):
            res = self.run_plan(self.plan(target="session"))
        self.assertIn("kept: could not kill session proj-feat-x: permission denied", res.message)
        self.assertIn("kept: kitty window 88 (shows the kept session proj-feat-x)", res.message)
        self.assertEqual(len(state.list_journals()), 1)

    def test_window_close_failure_is_kept(self):
        from agentree import state
        self.links.fail = True
        orig = GitWorld.kitty

        def no_close(world, argv):
            if argv[4] == "close-window":
                return R(argv, 1, err="no such window")
            return orig(world, argv)
        with mock.patch.object(GitWorld, "kitty", no_close), mock.patch.object(sp, "SESSION_WAIT_S", 2.0):
            res = self.run_plan(self.plan(target="session"))
        self.assertIn("rolled back:", res.message)                 # the session itself went
        self.assertIn("kept: could not close kitty window 88: no such window", res.message)
        self.assertEqual(len(state.list_journals()), 1)

    def test_launch_refused_leaves_nothing_unresolved(self):
        from agentree import state
        orig = GitWorld.kitty

        def refuse(world, argv):
            if argv[4] == "launch":
                return R(argv, 1, err="remote control disabled")
            return orig(world, argv)
        with mock.patch.object(GitWorld, "kitty", refuse):
            res = self.run_plan(self.plan(target="session"))
        self.assertIn("kitty launch: remote control disabled", res.message)
        self.assertNotIn("kept", res.message)
        self.assertEqual(state.list_journals(), [])


class TabIdPersistTest(SpawnBase):
    """Finding 4: the new tab's id is journaled before any further query."""

    def test_id_journaled_before_next_query_and_kept_on_listing_failure(self):
        order = []
        store = FlakyStore(log=order)
        orig = GitWorld.zellij
        broken = {"on": False}

        def z(world, argv, env):
            if argv[1] == "--session" and argv[4] in ("list-tabs", "list-panes", "list-clients"):
                order.append(("query", argv[4]))
                if broken["on"]:
                    return R(argv, 1, err="zellij: connection reset")
            r = orig(world, argv, env)
            if argv[1] == "--session" and argv[4] == "new-tab":
                order.append(("new-tab", None))
                broken["on"] = True                       # every listing fails from now on
            return r
        with mock.patch.object(GitWorld, "zellij", z):
            res = sp.execute_spawn(self.plan(), self.nav(current="OTHER"), cfg=self.cfg,
                                   state_dir=os.path.join(self.tmp, "state"), links=self.links,
                                   journal_store=store)
        self.assertFalse(res.ok)
        i = order.index(("new-tab", None))
        kind, data = order[i + 1]
        self.assertEqual(kind, "journal")                 # persisted before the next query
        tab = [e for e in data["entries"] if e["kind"] == "tab"][0]
        self.assertEqual((tab["state"], tab["tab_id"]), ("created", 1))
        self.assertIn("tab 1 created but not verified", res.message)
        # identity could not be checked (listing broken): the tab is kept, not forgotten
        self.assertIn("kept: tab 1 in S", res.message)
        self.assertIn((1, "feat-x"), self.w.s("S")["tabs"])


class MixedPanesTest(SpawnBase):
    """Finding 5: rollback only closes/kills what holds nothing but the spawned worktree."""

    def test_tab_with_a_foreign_pane_is_kept(self):
        def add_foreign(rec, persist_prev):
            self.w.s("S")["panes"].append((55, 1, "/Users/me/elsewhere", False))
            raise OSError("disk full")
        self.links.swap_link_journaled = add_foreign
        res = self.run_plan(self.plan())
        self.assertIn("also has panes outside the worktree (terminal_55:/Users/me/elsewhere)", res.message)
        self.assertNotIn("close-tab-by-id", [a[2] for a in self.w.acts() if a[0] == "zj"])

    def test_session_with_a_foreign_pane_is_kept(self):
        def add_foreign(rec, persist_prev):
            self.w.s("proj-feat-x")["panes"].append((56, 0, "/Users/me/other", False))
            raise OSError("disk full")
        self.links.swap_link_journaled = add_foreign
        res = self.run_plan(self.plan(target="session"))
        self.assertIn("kept: session proj-feat-x: it has panes outside the worktree", res.message)
        self.assertFalse(self.w.s("proj-feat-x")["exited"])

    def test_only_worktree_panes_still_rolled_back(self):
        def add_inside(rec, persist_prev):
            self.w.s("S")["panes"].append((57, 1, self.wt + "/sub", False))
            raise OSError("disk full")
        self.links.swap_link_journaled = add_inside
        res = self.run_plan(self.plan())
        self.assertIn("tab 1", res.message.split("rolled back:")[1])


# --------------------------------------------------------------------------- providers

class AgentLayoutTest(unittest.TestCase):
    """Spawn launch argv built by the real providers (build_launch(mode="spawn")), escaped by
    agent_layout."""

    def launch(self, provider, cfg, **kw):
        return registry.get(provider).build_launch(cfg, LaunchRequest(mode="spawn", **kw))

    def test_opencode_command_split_into_argv0_and_args(self):
        la = self.launch("opencode", Config(opencode_cmd="~/bin/opencode-auto --model 'x y'"), prompt="hi")
        lay = sp.agent_layout(la.argv, pane_name="opencode")
        self.assertIn(f'name="opencode" command="{os.path.expanduser("~/bin/opencode-auto")}" focus=true', lay)
        self.assertIn('args "--model" "x y" "--prompt" "hi";', lay)
        self.assertEqual((la.session_id, la.setup), (None, ()))
        # no prompt: no generated command, the named layout (the user's agent.kdl) is kept
        self.assertIsNone(self.launch("opencode", Config(opencode_cmd="oc"), layout="agent").argv)

    def test_claude_layout(self):
        cfg = Config(claude_cmd="/x/claude --model opus")
        la = self.launch("claude", cfg, prompt='-say "hi"\nthen \\ go', session_id="abc-123",
                         settings_path="/st/claude/settings.json")
        lay = sp.agent_layout(la.argv, pane_name="claude")
        self.assertNotIn("\n", lay)
        self.assertIn('pane size="65%" name="claude" command="/x/claude" focus=true', lay)
        self.assertIn('args "--model" "opus" "--settings" "/st/claude/settings.json" "--session-id" "abc-123" '
                      '"--" "-say \\"hi\\"\\nthen \\\\ go";', lay)
        self.assertEqual(la.session_id, "abc-123")
        la = self.launch("claude", Config(claude_cmd="/x/claude"), layout="agent", session_id="s1",
                         settings_path="/st/s.json")
        lay = sp.agent_layout(la.argv, pane_name="claude")
        self.assertIn('args "--settings" "/st/s.json" "--session-id" "s1";', lay)
        self.assertNotIn('"--"', lay)

    def test_launch_args(self):
        self.assertEqual(self.launch("opencode", Config(opencode_cmd="oc -m"), prompt="p").argv,
                         ("oc", "-m", "--prompt", "p"))
        la = self.launch("claude", Config(claude_cmd="/x/cc"), prompt="go", session_id="sid", settings_path="/s")
        self.assertEqual(la.argv, ("/x/cc", "--settings", "/s", "--session-id", "sid", "go"))


def _fake_exe(path):
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(path, 0o755)
    return path


class ClaudeSpawnTest(SpawnBase):
    def setUp(self):
        super().setUp()
        self.claude = _fake_exe(os.path.join(self.tmp, "claude"))
        self.cfg = replace(self.cfg, claude_cmd=self.claude)
        self.settings = os.path.join(self.tmp, "state", "claude", "settings.json")

    def test_tab_plan_with_prompt(self):
        plan = self.plan(agent="claude", prompt="-v please")
        self.assertEqual(plan.errors, ())
        kinds = [s["kind"] for s in plan.data["spec"]]
        self.assertEqual(kinds, ["exclude", "worktree", "env", "provider_setup", "tab", "links", "switch"])
        sid = plan.data["launch_session_id"]
        self.assertRegex(sid, r"^[0-9a-f-]{36}$")
        self.assertEqual(plan.data["provider"], "claude")
        self.assertNotIn("cc_session_id", plan.data)
        self.assertIn(f"write claude hook settings {self.settings}", plan.steps[3][0])
        st = plan.data["spec"][3]
        self.assertEqual((st["provider"], st["operation"], st["payload"]),
                         ("claude", "write_hook_settings", [["settings_path", self.settings]]))
        json.dumps(plan.data["spec"])                                   # plain data
        lay = plan.steps[4][plan.steps[4].index("--layout-string") + 1]
        self.assertIn(f'command="{self.claude}"', lay)
        self.assertIn(f'args "--settings" "{self.settings}" "--session-id" "{sid}" "--" "-v please";', lay)
        self.assertEqual(plan.data["agent_cmd"],
                         f"{self.claude} --settings {self.settings} --session-id {sid} <prompt>")
        self.assertFalse(os.path.exists(self.settings))                 # plan: side-effect free

    def test_agent_layout_without_prompt_starts_claude(self):
        plan = self.plan(agent="claude", layout="agent")
        self.assertEqual(plan.errors, ())
        tab = plan.steps[[s["kind"] for s in plan.data["spec"]].index("tab")]
        self.assertIn("--layout-string", tab)
        self.assertNotIn("--layout", tab)
        lay = tab[tab.index("--layout-string") + 1]
        self.assertIn(f'"--session-id" "{plan.data["launch_session_id"]}";', lay)
        self.assertEqual(plan.data["agent_cmd"],
                         f"{self.claude} --settings {self.settings} --session-id {plan.data['launch_session_id']}")
        # opencode keeps using the user's agent layout by name
        p2 = self.plan(agent="opencode", layout="agent")
        self.assertIn("--layout", p2.steps[[s["kind"] for s in p2.data["spec"]].index("tab")])
        self.assertIsNone(p2.data["launch_session_id"])
        self.assertEqual(p2.data["provider"], "opencode")

    def test_no_prompt_other_layout_warns(self):
        plan = self.plan(agent="claude", layout="default")
        self.assertEqual(plan.errors, ())
        self.assertIn("claude is not started", " ".join(plan.warnings))
        self.assertNotIn("provider_setup", [s["kind"] for s in plan.data["spec"]])
        self.assertIn("built-in agent layout", " ".join(self.plan(agent="claude", prompt="x", layout="default").warnings))

    def test_missing_claude_is_an_error(self):
        self.cfg = replace(self.cfg, claude_cmd="/nonexistent/claude")
        plan = self.plan(agent="claude", prompt="x")
        self.assertEqual([e for e in plan.errors if "claude command not found: '/nonexistent/claude'" in e],
                         [plan.errors[0]])                              # once
        self.assertEqual(self.plan(agent="opencode", prompt="x").errors, ())

    def test_unavailable_provider_blocks_only_when_launched(self):
        self.cfg = replace(self.cfg, opencode_cmd="/nonexistent/oc")
        self.assertIn("opencode command not found: '/nonexistent/oc'", " ".join(self.plan(prompt="x").errors))
        self.assertEqual(self.plan(layout="agent").errors, ())          # named layout: nothing launched by agentree

    def test_unknown_provider_is_refused(self):
        plan = self.plan(agent="gpt", prompt="x")
        self.assertIn("unknown agent provider 'gpt'", plan.errors)
        self.assertEqual(self.run_plan(plan).ok, False)

    def test_session_plan_layout_file(self):
        plan = self.plan(agent="claude", target="session", prompt='hi "x"')
        self.assertEqual(plan.errors, ())
        lf_step = next(s for s, x in zip(plan.steps, plan.data["spec"]) if x["kind"] == "layout_file")
        self.assertIn(f"write the agent layout ({self.claude} --settings", lf_step[0])
        content = next(x for x in plan.data["spec"] if x["kind"] == "layout_file")["content"]
        self.assertIn('"hi \\"x\\""', content)

    def test_execute_writes_settings_and_polls_by_session_id(self):
        plan = self.plan(agent="claude", prompt="go")
        seen = {}

        def finder(_self, s, ids, *, session_id, timeout_s):
            seen["finder"] = (_self.id, session_id)
            return (4343,)
        prov = registry.get("claude")
        with mock.patch.object(type(prov), "find_launched_pids", autospec=True, side_effect=finder), \
                mock.patch("agentree.providers.claude.new_session_id", side_effect=AssertionError("regenerated")):
            res = sp.execute_spawn(plan, self.nav(current="OTHER"), cfg=self.cfg,
                                   state_dir=os.path.join(self.tmp, "state"), progress=self.events.append,
                                   links=self.links)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(seen["finder"], ("claude", plan.data["launch_session_id"]))
        self.assertEqual(self.links.records[0]["agentPids"], [4343])
        with open(self.settings) as fh:
            self.assertIn("claude-hook", fh.read())

    def test_dry_run_writes_no_settings(self):
        plan = self.plan(agent="claude", prompt="go")
        res = self.run_plan(plan, nav=self.nav(current="OTHER", dry_run=True))
        self.assertTrue(res.ok, res.message)
        self.assertFalse(os.path.exists(self.settings))

    def test_options_default_agent(self):
        o = sp.spawn_options(None, self.snap, replace(self.cfg, default_agent="claude"))
        self.assertEqual((o.agents, o.default_provider),
                         ((AgentChoice("opencode", "opencode", True), AgentChoice("claude", "Claude Code", True)),
                          "claude"))
        self.assertEqual(sp.spawn_options(None, self.snap, self.cfg).default_provider, "opencode")
        self.assertEqual(sp.spawn_options(None, self.snap, replace(self.cfg, default_agent="gpt")).default_provider,
                         "opencode")
        o = sp.spawn_options(None, self.snap, replace(self.cfg, claude_cmd="/nonexistent/claude"))
        self.assertEqual(o.agents[1], AgentChoice("claude", "Claude Code", False))

    def test_cli_agent_flag_and_default(self):
        import io

        from agentree import cli
        from agentree.actions.real import RealActions

        a = RealActions(self.cfg, dry_run=True, run_fn=self.w, environ={"ZELLIJ_SOCKET_DIR": DIR},
                        sockets=lambda: [SOCK], stream_fn=self.stream, links=self.links,
                        nav_overrides=fake_nav(self.w, clock=lambda: NOW, owned=lambda s: True,
                                               socket_dirs=lambda: [DIR], zellij=ZJ, kitty=KT))
        ns = cli._parser().parse_args(["spawn", "proj", "agent/feat-x", "--agent", "claude", "--prompt", "hi",
                                       "--dry-run"])
        self.assertEqual(ns.agent, "claude")
        out = io.StringIO()
        with mock.patch("agentree.config.load_config", return_value=self.cfg):
            code = cli.cmd_spawn(ns, dry_run=True, actions=a, snapshot=self.snap, out=out)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("write claude hook settings", text)
        self.assertIn(f"command=\"{self.claude}\"", text)
        self.assertIn("--session-id", text)
        self.assertFalse(os.path.exists(self.settings))
        self.assertFalse(os.path.exists(self.wt))
        # without --agent: config default_agent
        ns = cli._parser().parse_args(["spawn", "proj", "agent/feat-x", "--prompt", "hi"])
        self.assertIsNone(ns.agent)
        self.assertEqual(cli.spawn_agent(ns, replace(self.cfg, default_agent="claude")), "claude")
        self.assertEqual(cli.spawn_agent(ns, self.cfg), "opencode")
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli._parser().parse_args(["spawn", "proj", "b", "--agent", "gpt"])


class FindAgentsTest(unittest.TestCase):
    """provider.find_launched_pids (the spawn / `o` pid poll) with the process layer mocked."""

    def run_find(self, procs, provider, sid, pane_ids=frozenset({7, 8})):
        argv_env = {pid: (argv, env) for pid, _pp, argv, env in procs}
        rows = [(pid, pp, " ".join(argv)) for pid, pp, argv, _e in procs]
        from agentree.sources import proc
        with mock.patch.object(proc, "list_procs", return_value=rows), \
                mock.patch.object(proc, "proc_args_env", side_effect=lambda pid: argv_env.get(pid)):
            find = sp_pid_finder(registry.get(provider), sid)
            return find("S", set(pane_ids))

    def test_claude_prefers_our_session_id(self):
        env7 = {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "7"}
        procs = [
            (100, 1, ("/u/.local/bin/claude", "--settings", "/s", "--session-id", "mine", "fix the bug"), env7),
            (101, 1, ("claude", "--resume", "old"), {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "8"}),
            (102, 1, ("claude", "--session-id", "mine"), {"ZELLIJ_SESSION_NAME": "T", "ZELLIJ_PANE_ID": "7"}),
            (103, 1, ("opencode",), env7),
            (104, 1, ("claude", "mcp", "serve"), env7),
        ]
        self.assertEqual(self.run_find(procs, "claude", "mine"), [100])
        self.assertEqual(self.run_find(procs, "claude", "other"), [100, 101])     # none ours: all in the panes
        self.assertEqual(self.run_find(procs, "claude", None), [100, 101])
        self.assertEqual(self.run_find(procs, "opencode", None), [103])
        self.assertEqual(self.run_find(procs, "claude", "mine", pane_ids={9}), [])


def sp_pid_finder(provider, sid):
    from agentree.actions.agent import pid_finder

    return pid_finder(provider, sid)


class FakeThirdProviderSpawnTest(SpawnBase):
    """A provider injected without core edits: spawn plans its setup steps (generic
    provider_setup, before the tab), its launch argv in the generated layout, the session id
    generated at plan time, dry-run, and the pid poll."""

    def setUp(self):
        super().setUp()
        self.codex = FakeProvider(pids=[5151])
        inject(self, self.codex)
        self.cfg = Config(repo_roots=self.cfg.repo_roots, install_commands=self.cfg.install_commands,
                          opencode_cmd=self.oc, provider_commands={"codex": f"{self.oc} --full-auto"})
        self.settings = os.path.join(self.tmp, "state", "codex", "settings.json")

    def test_plan(self):
        plan = self.plan(agent="codex", prompt="go", run_install=True)
        self.assertEqual(plan.errors, ())
        kinds = [s["kind"] for s in plan.data["spec"]]
        self.assertEqual(kinds, ["exclude", "worktree", "env", "install", "provider_setup", "tab", "links", "switch"])
        self.assertEqual((plan.data["provider"], plan.data["launch_session_id"]), ("codex", "codex-sid-1"))
        st = plan.data["spec"][4]
        self.assertEqual((st["provider"], st["operation"], st["payload"]),
                         ("codex", "write_settings", [["path", self.settings]]))
        self.assertEqual(plan.steps[4], (f"write codex settings {self.settings}",))
        lay = plan.steps[5][plan.steps[5].index("--layout-string") + 1]
        self.assertIn(f'name="codex" command="{self.oc}" focus=true {{ args "--full-auto" "--session" '
                      f'"codex-sid-1" "--" "go"; }}', lay)
        self.assertEqual(plan.data["agent_cmd"], f"{self.oc} --full-auto --session codex-sid-1 -- <prompt>")
        req = self.codex.calls[0][1]
        self.assertEqual((req.mode, req.prompt, req.layout), ("spawn", "go", None))
        # without a prompt the fake starts nothing: the named layout is kept, its warning shown
        p2 = self.plan(agent="codex", layout="agent")
        self.assertIn("no prompt: codex is not started", p2.warnings)
        self.assertIn("--layout", p2.steps[[s["kind"] for s in p2.data["spec"]].index("tab")])
        self.assertNotIn("provider_setup", [s["kind"] for s in p2.data["spec"]])

    def test_unavailable_is_a_blocking_error(self):
        self.cfg = replace(self.cfg, provider_commands={"codex": "/nonexistent/codex"})
        plan = self.plan(agent="codex", prompt="go")
        self.assertIn("codex command not found: '/nonexistent/codex' (set codex_cmd in ~/.config/agentree/config.toml)",
                      plan.errors)

    def test_execute_runs_setup_once_and_polls_its_pids(self):
        plan = self.plan(agent="codex", prompt="go")
        n_builds = len([c for c in self.codex.calls if c[0] == "build_launch"])
        res = sp.execute_spawn(plan, self.nav(current="OTHER"), cfg=self.cfg,
                               state_dir=os.path.join(self.tmp, "state"), progress=self.events.append,
                               links=self.links)
        self.assertTrue(res.ok, res.message)
        self.assertTrue(os.path.exists(self.settings))
        setup = [c for c in self.codex.calls if c[0] == "execute_setup"]
        self.assertEqual(len(setup), 1)
        self.assertEqual(setup[0][1].payload, (("path", self.settings),))
        finds = [c for c in self.codex.calls if c[0] == "find_launched_pids"]
        self.assertEqual(finds[0][3], "codex-sid-1")                    # plan-time session id
        self.assertEqual(len([c for c in self.codex.calls if c[0] == "build_launch"]), n_builds)   # not rebuilt
        self.assertEqual(self.links.records[0]["agentPids"], [5151])
        k = [x["kind"] for x in plan.data["spec"]].index("provider_setup")
        self.assertIn("settings written", [e.detail for e in self.events if e.index == k and e.state == "ok"])

    def test_dry_run(self):
        plan = self.plan(agent="codex", prompt="go")
        res = self.run_plan(plan, nav=self.nav(current="OTHER", dry_run=True))
        self.assertTrue(res.ok, res.message)
        self.assertFalse(os.path.exists(self.settings))
        self.assertEqual(len([c for c in self.codex.calls if c[0] == "execute_setup"]), 1)   # through the runner

    def test_options_and_cli(self):
        o = sp.spawn_options(None, self.snap, self.cfg)
        self.assertEqual(o.agents[-1], AgentChoice("codex", "Codex", True))
        from agentree import cli
        ns = cli._parser().parse_args(["spawn", "proj", "agent/x", "--agent", "codex", "--prompt", "hi"])
        self.assertEqual((ns.agent, cli.spawn_agent(ns, self.cfg)), ("codex", "codex"))
        self.assertEqual(cli.spawn_agent(cli._parser().parse_args(["spawn", "proj", "b"]),
                                         replace(self.cfg, default_agent="codex")), "codex")
