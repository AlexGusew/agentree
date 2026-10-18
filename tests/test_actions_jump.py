"""Jump action: exact command sequences against a fake zellij/kitty (no real side effects)."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from agentree.actions import exec as ex
from agentree.actions.jump import Env, Navigator, already_focused, resolve
from agentree.actions.api import Plan
from agentree.actions.real import RealActions
from agentree.actions.runner import Runner
from agentree.sources import zellij as zsrc
from agentree.types import (
    Agent, KittyHost, Orphans, Row, SessionNode, Snapshot, Worktree, WorktreeNode, ZPane, ZSession, ZTab,
)

ZJ = "/opt/homebrew/bin/zellij"
KT = "/Applications/kitty.app/Contents/MacOS/kitty"
SOCK = "unix:/tmp/kitty.sock-111"
SOCK2 = "unix:/tmp/kitty.sock-222"
DIR = "/tmp/zellij-502"
DIR2 = "/tmp/zellij-alt"
TAKEN = 1000.0
NOW = 1010.0
AGE = 60          # snapshot age of every session; the fake reports AGE + 10 at NOW


def R(argv, code=0, out="", err="", dry=False):
    return ex.Result(tuple(argv), code, out, err, False, 0.0, dry)


LIST_SESSIONS = zsrc.list_sessions_argv(ZJ)     # /usr/bin/env -u ZELLIJ -u … zellij list-sessions -n


def unwrap_list_sessions(argv):
    """Shared by every fake runner: the wrapped `env -u … zellij list-sessions -n` is answered as
    plain `zellij list-sessions -n`; an unwrapped list-sessions is a bug (it would inherit the
    caller's ZELLIJ* env) and fails the test."""
    argv = list(argv)
    if argv == LIST_SESSIONS:
        return [ZJ, "list-sessions", "-n"]
    if argv[:1] == [ZJ] and argv[1:2] == ["list-sessions"]:
        raise AssertionError(f"list-sessions without the env -u wrapper: {argv}")
    return argv


def fake_nav(world, **overrides):
    """nav_overrides for RealActions in tests: the socket check follows the fake world."""
    return {"has_socket": world.has_socket, **overrides}


class World:
    """Fake zellij sessions + kitty windows. Records every command as (argv, env, dry_run)."""

    def __init__(self):
        self.sessions = {}      # (dir, name) -> dict(tabs, panes, clients, age, exited)
        self.windows = {SOCK: []}
        self.calls = []
        self.focus_result = None
        self.new_tab_out = None
        self.new_tab_name = None    # override the created tab's name
        self.attach_clients = 1
        self.on_attach = None       # callback(world, session) run when kitty attaches
        self.clients_fail = False
        self.launched = []
        self.agent_env = {}         # pid -> env dict
        self.proc_start = {}        # pid -> process start time (unknown if absent)
        self.phantom = set()        # (dir, name) listed live without a socket (zellij env leak)
        self.dead = set()
        # focus model (list-clients ZELLIJ_PANE_ID is the truth, as in zellij 0.45.1)
        self.active = {}            # session -> active tab id (default: first tab)
        self.focused = {}           # session -> focused pane id (unset: derived from the tab's layers)
        self.tab_focus = {}         # (session, tab) -> remembered tiled focus
        self.float_focus = {}       # (session, tab) -> remembered floating focus
        self.layer = {}             # (session, tab) -> floating layer shown
        self.floating = set()       # floating terminal pane ids
        self.floating_plugins = set()   # floating plugin pane ids (share the layer)
        self.focus_tiled_noop = False   # zellij 0.45.1: focus-pane-id on a tiled pane does nothing
        self.unreachable = set()    # panes focus-next-pane skips (collapsed in a stack)
        self.focus_pane_calls = []

    def _active(self, name, s):
        return self.active.get(name, s["tabs"][0][0] if s["tabs"] else None)

    def _tab_panes(self, s, tab):
        return [p for p, t, _c, pl in s["panes"] if t == tab and not pl]

    def _tiled(self, s, tab):
        return [p for p in self._tab_panes(s, tab) if p not in self.floating]

    def _floats(self, s, tab):
        return [p for p in self._tab_panes(s, tab) if p in self.floating]

    def _focused(self, name, s):
        if name in self.focused:
            return self.focused[name]
        tab = self._active(name, s)
        if self.layer.get((name, tab)) and self._floats(s, tab):
            return self.float_focus.get((name, tab), self._floats(s, tab)[0])
        return self.tab_focus.get((name, tab), next(iter(self._tiled(s, tab)), 0))

    def _focus(self, name, s, pid):
        """Focus `pid` the way zellij does: a floating pane shows its tab's layer, a tiled one
        is focused with the layer hidden."""
        tab = next((t for p, t, _c, _pl in s["panes"] if p == pid), None)
        self.focused[name] = pid
        if tab is not None:
            self.active[name] = tab
            if pid in self.floating:
                self.layer[(name, tab)] = True
                self.float_focus[(name, tab)] = pid
            else:
                self.layer[(name, tab)] = False
                self.tab_focus[(name, tab)] = pid

    def show_floating(self, name, pid):
        """A floating pane focused in its tab (shown layer), as a test precondition."""
        self._focus(name, self.s(name), pid)

    def add_session(self, name, tabs=(), panes=(), clients=1, *, d=DIR, age=AGE + 10, exited=False):
        self.sessions[(d, name)] = {"tabs": list(tabs), "panes": list(panes), "clients": clients,
                                    "age": age, "exited": exited}

    def s(self, name, d=DIR):
        return self.sessions[(d, name)]

    def window(self, wid, session, title=None, overridden=False, argv=None, user_vars=None, sock=SOCK):
        self.windows.setdefault(sock, []).append({
            "id": wid, "title": f"{session} | fish" if title is None else title,
            "title_overridden": overridden, "is_focused": False, "user_vars": user_vars or {},
            "foreground_processes": [{"cmdline": argv or [ZJ, "attach", session]}]})

    # ---------------------------------------------------------------- dispatch
    def __call__(self, argv, *, env=None, timeout=1.0, log=False, dry_run=False):
        self.calls.append((tuple(argv), dict(env or {}), dry_run))
        if dry_run:
            return R(argv, dry=True)
        argv = unwrap_list_sessions(argv)
        if argv[0] == ZJ:
            return self.zellij(argv, env or {})
        if argv[0] == KT:
            return self.kitty(argv)
        return R(argv, 127, err="unknown")

    def has_socket(self, d, name):
        """Navigator.has_socket stub: a server socket exists exactly for the fake's live sessions
        (phantom listings have none)."""
        s = self.sessions.get((d, name))
        return s is not None and not s["exited"]

    def zellij(self, argv, env):
        d = env.get("ZELLIJ_SOCKET_DIR", DIR)
        if argv[1] == "list-sessions":
            rows = [f"{n} [Created {v['age']}s ago] " + ("(EXITED - attach to resurrect)" if v["exited"] else "")
                    for (dd, n), v in self.sessions.items() if dd == d]
            # zellij 0.45.1 inside session n: n listed live "(current)" in a dir it does not run in
            rows += [f"{n} [Created {AGE + 10}s ago] (current)" for dd, n in self.phantom if dd == d]
            if not rows:
                return R(argv, 1, err="No active zellij sessions found.")
            return R(argv, out="\n".join(rows) + "\n")
        assert argv[1] == "--session" and argv[3] == "action", argv
        name, cmd, args = argv[2], argv[4], argv[5:]
        s = self.sessions.get((d, name))
        if s is None or s["exited"]:
            return R(argv, 1, err=f"session {name} not found")
        if cmd == "list-tabs":
            return R(argv, out=json.dumps([{"tab_id": t, "position": i, "name": n, "active": False,
                                            "are_floating_panes_visible": bool(self.layer.get((name, t)))}
                                           for i, (t, n) in enumerate(s["tabs"])]))
        if cmd == "list-panes":
            return R(argv, out=json.dumps([{"id": p, "tab_id": t, "pane_cwd": c, "is_plugin": pl,
                                            "title": "", "terminal_command": None,
                                            "is_floating": (p in self.floating_plugins) if pl else (p in self.floating)}
                                           for p, t, c, pl in s["panes"]]))
        if cmd == "list-clients":
            if self.clients_fail:
                return R(argv, 1, err="boom")
            f = self._focused(name, s)
            rows = "".join(f"{i + 1}  terminal_{f}  N/A\n" for i in range(s["clients"]))
            return R(argv, out="CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n" + rows)
        if cmd == "go-to-tab-by-id":
            tab = int(args[0])
            if any(t == tab for t, _n in s["tabs"]) and tab != self._active(name, s):
                self.active[name] = tab
                self.focused.pop(name, None)
            return R(argv)
        if cmd == "focus-pane-id":
            self.focus_pane_calls.append(args[0])
            pid = int(args[0].rsplit("_", 1)[-1])
            if self.focus_result:
                if self.focus_result[0] == 2 and f"Terminal({pid}) is already focused" in self.focus_result[1]:
                    self._focus(name, s, pid)
                return R(argv, self.focus_result[0], err=self.focus_result[1])
            if args[0].startswith("plugin_"):
                return R(argv)
            if pid in self.floating:
                self._focus(name, s, pid)                # floating panes do take focus
                return R(argv)
            tab = self._active(name, s)
            if self.layer.get((name, tab)) and self._floats(s, tab):
                # zellij 0.45.1 (live): a tiled target while the floating layer has focus hides
                # the whole layer; focus falls to the tab's last tiled pane
                self.layer[(name, tab)] = False
                self.focused.pop(name, None)
            if not self.focus_tiled_noop:
                self._focus(name, s, pid)
            return R(argv)
        if cmd == "focus-next-pane":
            cur = self._focused(name, s)
            if cur in self.floating:
                return R(argv)                       # stays in the floating layer
            ring = [p for p in self._tiled(s, self._active(name, s))
                    if p not in self.unreachable or p == cur]
            if cur in ring:
                nxt = ring[(ring.index(cur) + 1) % len(ring)]
                self._focus(name, s, nxt)
            return R(argv)
        if cmd == "toggle-floating-panes":
            tab = self._active(name, s)
            self.focused.pop(name, None)
            if self.layer.get((name, tab)):          # hide the whole layer: focus falls to the tiled one
                self.layer[(name, tab)] = False
            elif self._floats(s, tab):               # show it again: its last focused pane takes the focus
                self.layer[(name, tab)] = True
            return R(argv)
        if cmd == "new-tab":
            if s["clients"] == 0:
                raise AssertionError("new-tab with 0 clients")
            if self.new_tab_out is not None:
                return R(argv, out=self.new_tab_out)
            new_id = max([t for t, _ in s["tabs"]] + [-1]) + 1
            cwd = args[args.index("--cwd") + 1]
            s["tabs"].append((new_id, self.new_tab_name or args[args.index("--name") + 1]))
            s["panes"].append((100 + new_id, new_id, cwd, False))
            if "--no-focus" not in args:
                self._focus(name, s, 100 + new_id)
            return R(argv, out=f"{new_id}\n")
        return R(argv, 1, err=f"unknown action {cmd}")

    def kitty(self, argv):
        sock, cmd = argv[3], argv[4]
        if cmd == "ls":
            return R(argv, out=json.dumps([{"id": 1, "is_focused": True, "tabs": [
                {"id": 1, "is_focused": True, "windows": self.windows.get(sock, [])}]}]))
        if cmd == "focus-window":
            return R(argv)
        if cmd == "launch":
            name = argv[-1]
            d = argv[argv.index("--env") + 1].split("=", 1)[1]
            self.launched.append(name)
            s = self.sessions.get((d, name))
            if s is not None:
                s["clients"] = self.attach_clients
                s["exited"] = False
            if self.on_attach:
                self.on_attach(self, name)
            var = argv[argv.index("--var") + 1].split("=", 1)[1]
            self.window(77, name, title="zellij", user_vars={"agentree_session": var}, sock=sock)
            return R(argv, out="77\n")
        return R(argv, 1, err="unknown")

    # ---------------------------------------------------------------- views
    def acts(self):
        out = []
        for argv, env, _dry in self.calls:
            if argv[0] == ZJ and argv[1] == "--session" and argv[4] not in ("list-tabs", "list-panes", "list-clients"):
                out.append(("zj", argv[2], *argv[4:]))
            elif argv[0] == KT and argv[4] != "ls":
                out.append(("kitty", argv[3], *argv[4:]))
        return out

    def index(self, pred):
        return next(i for i, (a, _e, _d) in enumerate(self.calls) if pred(a))

    def last_index(self, pred):
        return max(i for i, (a, _e, _d) in enumerate(self.calls) if pred(a))


def session(name, *, state="attached", clients=1, complete=True, socket_dir=DIR, tabs=(), age=AGE):
    return ZSession(name, state, age, socket_dir if state != "exited" else None, clients, False, tuple(tabs),
                    complete)


def pane(pid, tab, cwd, cmd="fish", plugin=False):
    return ZPane(pid, plugin, tab, cmd, cmd, (cmd,), cwd)


def row(kind, ref, session_name, key="k"):
    return Row(key=key, kind=kind, depth=0, caret=None, icon=None, label=(), meta=(), ref=ref, session=session_name)


WT = Worktree("/r/.worktrees/x", "/r", "agent/x", "abc", False)
ROOT = Worktree("/r", "/r", "main", "abc", True)
AGENT_PID = 4242


def make_world_and_snap(*, name="S", state="attached", clients=1, complete=True, host=True, wt_tab=True):
    w = World()
    tabs = (ZTab(0, 0, "main", True, (pane(0, 0, "/r"),)),
            ZTab(1, 1, "x", False, (pane(1, 1, WT.path), pane(2, 1, WT.path, "opencode"))))
    if state != "exited":
        w.add_session(name, [(0, "main"), (1, "x")],
                      [(0, 0, "/r", False), (1, 1, WT.path, False), (2, 1, WT.path, False), (9, 0, None, True)],
                      clients)
    else:
        w.add_session(name, exited=True, clients=0)
    w.agent_env[AGENT_PID] = {"ZELLIJ_SESSION_NAME": name, "ZELLIJ_PANE_ID": "2"}
    s = session(name, state=state, clients=clients, complete=complete, tabs=tabs if state != "exited" else ())
    kh = KittyHost(SOCK, 1, 1, 5, name) if host else None
    if host:
        w.window(5, name)
    wn = WorktreeNode(WT, tabs=(tabs[1],) if wt_tab else ())
    agent = Agent(pid=AGENT_PID, cwd=WT.path, meta={"port": 5000}, session_name=name, pane_id=2)
    wn = replace(wn, agents=(agent,))
    node = SessionNode(s, (WorktreeNode(ROOT, tabs=(tabs[0],)), wn), kitty=kh)
    snap = Snapshot(TAKEN, (node,), Orphans())
    return w, snap, node


def nav_for(world, *, current=None, current_dir=DIR, dry_run=False, listen_on=None, sockets=(SOCK,),
            socket_dir=DIR, dirs=(DIR,), owned=None):
    env = Env(current, current_dir if current else socket_dir, listen_on, 502, "/tmp")
    return Navigator(runner=Runner(dry_run, world), env=env, zellij=ZJ, kitty=KT,
                     sockets=lambda: list(sockets), socket_dirs=lambda: list(dirs),
                     owned=owned or (lambda s: s in (SOCK, SOCK2)),
                     proc_env=lambda pid: world.agent_env.get(pid), pid_alive=lambda pid: pid not in world.dead,
                     proc_start=lambda pid: world.proc_start.get(pid),
                     has_socket=world.has_socket,
                     sleep=lambda s: None, clock=lambda: NOW, attach_wait_s=0.3)


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.w, self.snap, self.node = make_world_and_snap()

    def test_kinds(self):
        t = resolve(row("session", self.node, "S"), self.snap)
        self.assertEqual((t.tab_id, t.pane, t.taken_at), (None, None, TAKEN))
        t = resolve(row("worktree", self.node.worktrees[1], "S"), self.snap)
        self.assertEqual((t.tab_id, t.pane, t.worktree), (1, None, WT.path))
        self.assertIn("/r", t.roots)
        t = resolve(row("agent", self.node.worktrees[1].agents[0], "S"), self.snap)
        self.assertEqual((t.tab_id, t.pane, t.agent_pid), (1, "terminal_2", AGENT_PID))
        t = resolve(row("pane", self.node.session.tabs[0].panes[0], "S"), self.snap)
        self.assertEqual((t.tab_id, t.pane), (0, "terminal_0"))

    def test_messages(self):
        self.assertIn("press t", resolve(row("worktree", WorktreeNode(WT), None), self.snap))
        self.assertIn("press t", resolve(row("group", Orphans(), None), self.snap))
        self.assertIn("press t to open one", resolve(row("worktree", WorktreeNode(WT), "S"), self.snap))
        loose = Agent(pid=1, cwd=None, session_name=None, pane_id=None)
        self.assertIn("no zellij pane", resolve(row("agent", loose, None), self.snap))
        self.assertEqual(resolve(row("footer", None, None), self.snap), "nothing to jump to")


class JumpBase(unittest.TestCase):
    def go(self, w, snap, r, **kw):
        t = resolve(r, snap)
        self.assertNotIsInstance(t, str, t)
        return nav_for(w, **kw).go(t)


class JumpTest(JumpBase):
    def test_current_session_zellij_only(self):
        w, snap, node = make_world_and_snap()
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1"), ("zj", "S", "focus-pane-id", "terminal_2")])
        self.assertTrue(all(e == {"ZELLIJ_SOCKET_DIR": DIR} for a, e, _ in w.calls if a[0] == ZJ))

    def test_other_session_with_host(self):
        w, snap, node = make_world_and_snap()
        res = self.go(w, snap, row("pane", node.session.tabs[1].panes[0], "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)
        # terminal_1 is the tab's focused pane already (read back first): no focus-pane-id
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1"), ("kitty", SOCK, "focus-window", "--match", "id:5")])
        ls_i = w.index(lambda a: a[0] == KT and a[4] == "ls")
        goto_i = w.index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "go-to-tab-by-id")
        self.assertLess(ls_i, goto_i)
        # fresh identity check (list-sessions) happens before anything else
        self.assertEqual(w.calls[0][0], tuple(LIST_SESSIONS))

    def test_session_row_with_host_no_zellij_actions(self):
        w, snap, node = make_world_and_snap(clients=3)
        res = self.go(w, snap, row("session", node, "S"), current="X")
        self.assertEqual(w.acts(), [("kitty", SOCK, "focus-window", "--match", "id:5")])
        self.assertTrue(res.close)

    def test_detached_no_host_launches_and_waits(self):
        w, snap, node = make_world_and_snap(clients=0, host=False)
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), listen_on=SOCK2, sockets=(SOCK, SOCK2))
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(w.acts(), [
            ("kitty", SOCK2, "launch", "--type=tab", "--tab-title", "S", "--var", "agentree_session=S",
             "--env", f"ZELLIJ_SOCKET_DIR={DIR}", ZJ, "attach", "S"),
            ("zj", "S", "go-to-tab-by-id", "1"), ("zj", "S", "focus-pane-id", "terminal_2"),
            ("kitty", SOCK2, "focus-window", "--match", "id:77")])

    def test_attach_timeout(self):
        w, snap, node = make_world_and_snap(clients=0, host=False)
        w.attach_clients = 0
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"))
        self.assertFalse(res.ok)
        self.assertIn("no zellij client appeared", res.message)
        self.assertNotIn("go-to-tab-by-id", [a[2] for a in w.acts() if a[0] == "zj"])

    def test_attached_outside_kitty(self):
        w, snap, node = make_world_and_snap(clients=1, host=False)
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"))
        self.assertTrue(res.ok)
        self.assertFalse(res.close)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1")])
        self.assertEqual(w.launched, [])
        res = self.go(w, snap, row("session", node, "S"))
        self.assertFalse(res.ok)
        self.assertIn("outside kitty", res.message)

    def test_exited_resurrects(self):
        w, snap, node = make_world_and_snap(state="exited", host=False)
        res = self.go(w, snap, row("session", node, "S"), socket_dir="/tmp/zellij-502")
        self.assertTrue(res.ok and res.close)
        self.assertEqual(w.acts(), [("kitty", SOCK, "launch", "--type=tab", "--tab-title", "S", "--var",
                                     "agentree_session=S", "--env", "ZELLIJ_SOCKET_DIR=/tmp/zellij-502", ZJ, "attach", "S")])

    def test_already_focused_exit2(self):
        w, snap, node = make_world_and_snap()
        w.focus_result = (2, "Pane Terminal(2) is already focused\n")
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertTrue(res.ok, res.message)
        for err in ("Pane Terminal(7) is already focused", "Pane with id Terminal(2) not found"):
            w, snap, node = make_world_and_snap()          # pane 2 not focused yet
            w.focus_result = (2, err)
            self.assertFalse(self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S").ok)

    def test_already_focused_helper(self):
        self.assertTrue(already_focused(R(["x"], 2, err="Pane Terminal(3) is already focused"), "terminal_3"))
        self.assertFalse(already_focused(R(["x"], 1, err="Pane Terminal(3) is already focused"), "terminal_3"))
        self.assertFalse(already_focused(R(["x"], 2, err="Pane Terminal(3) is already focused!"), "terminal_3"))
        self.assertFalse(already_focused(R(["x"], 2, err="Pane Plugin(3) is already focused"), "plugin_3"))

    def test_inventory_incomplete_refused(self):
        w, snap, node = make_world_and_snap(complete=False)
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="OTHER")
        self.assertFalse(res.ok)
        self.assertIn("inventory incomplete", res.message)
        self.assertEqual(w.acts(), [])

    def test_fresh_listing_inconsistent_refused(self):
        w, snap, node = make_world_and_snap()
        w.s("S")["panes"].append((50, 99, "/x", False))
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("inventory incomplete", res.message)
        self.assertEqual(w.acts(), [])

    def test_stale_tab_and_moved_pane(self):
        w, snap, node = make_world_and_snap()
        w.s("S")["tabs"] = [(0, "main")]
        w.s("S")["panes"] = [(0, 0, "/r", False), (2, 0, WT.path, False)]
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("tab 1 is gone", res.message)
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "0"), ("zj", "S", "focus-pane-id", "terminal_2")])

    def test_ambiguous_kitty_socket(self):
        w, snap, node = make_world_and_snap(clients=0, host=False)
        res = self.go(w, snap, row("session", node, "S"), sockets=(SOCK, SOCK2))
        self.assertFalse(res.ok)
        self.assertIn("several kitty instances", res.message)
        self.assertEqual(w.acts(), [])
        res = self.go(w, snap, row("session", node, "S"), sockets=(SOCK, SOCK2), listen_on="unix:/tmp/kitty.sock-9")
        self.assertFalse(res.ok)
        res = self.go(w, snap, row("session", node, "S"), sockets=())
        self.assertIn("no kitty remote-control socket", res.message)

    def test_host_revalidation(self):
        w, snap, node = make_world_and_snap()
        w.windows[SOCK] = []
        w.window(5, "S", title="OTHER | fish")
        res = self.go(w, snap, row("session", node, "S"), current="X")
        self.assertFalse(res.ok)
        self.assertIn("no longer shows S", res.message)
        self.assertEqual(w.acts(), [])
        # window gone and no other host: S has a client elsewhere -> navigate only
        w.windows[SOCK] = []
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="X")
        self.assertTrue(res.ok)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1")])

    def test_host_moved_to_another_window_found_fresh(self):
        w, snap, node = make_world_and_snap()
        w.windows[SOCK] = []
        w.window(9, "S")                      # cached window 5 closed, S now in window 9
        res = self.go(w, snap, row("session", node, "S"), current="X")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts(), [("kitty", SOCK, "focus-window", "--match", "id:9")])

    def test_host_launched_by_agentree_with_default_title(self):
        w, snap, node = make_world_and_snap()
        w.windows[SOCK] = []
        w.window(5, "S", title="zellij", user_vars={"agentree_session": "S"})
        res = self.go(w, snap, row("session", node, "S"), current="X")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts(), [("kitty", SOCK, "focus-window", "--match", "id:5")])


class ClientSafetyTest(JumpBase):
    def test_failed_client_query_refuses(self):
        w, snap, node = make_world_and_snap()
        w.clients_fail = True
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("list-clients failed", res.message)
        self.assertEqual(w.acts(), [])
        w2, snap2, node2 = make_world_and_snap(clients=0, host=False)
        w2.clients_fail = True                # must not be read as "0 clients -> launch"
        res = self.go(w2, snap2, row("session", node2, "S"))
        self.assertFalse(res.ok)
        self.assertEqual(w2.launched, [])

    def test_multi_client_refuses_navigation_and_keeps_popup(self):
        for current in ("S", "OTHER"):
            w, snap, node = make_world_and_snap(clients=2)
            res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current=current)
            self.assertFalse(res.ok)
            self.assertFalse(res.close)
            self.assertIn("2 clients", res.message)
            self.assertEqual(w.acts(), [])          # no zellij nav, no kitty focus

    def test_clients_requeried_right_before_navigation(self):
        w, snap, node = make_world_and_snap(clients=1)          # snapshot says 1
        w.s("S")["clients"] = 2                                 # now 2
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        w.s("S")["clients"] = 1
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertTrue(res.ok)
        last_clients = w.last_index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "list-clients")
        goto = w.last_index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "go-to-tab-by-id")
        last_listing = w.last_index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "list-panes")
        self.assertLess(last_listing, last_clients)
        self.assertEqual(last_clients + 1, goto)

    def test_two_clients_after_attach_refuses_navigation(self):
        w, snap, node = make_world_and_snap(clients=0, host=False)
        w.attach_clients = 2
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"))
        self.assertFalse(res.ok)
        self.assertNotIn("go-to-tab-by-id", [a[2] for a in w.acts() if a[0] == "zj"])


class IdentityTest(JumpBase):
    def test_live_in_two_dirs_refused(self):
        w, snap, node = make_world_and_snap()
        w.add_session("S", d=DIR2, tabs=[(0, "t")])
        res = self.go(w, snap, row("session", node, "S"), dirs=(DIR, DIR2))
        self.assertFalse(res.ok)
        self.assertIn("several socket dirs", res.message)
        self.assertEqual(w.acts(), [])

    def test_exited_in_one_dir_live_in_other_is_fine(self):
        w, snap, node = make_world_and_snap()
        w.add_session("S", d=DIR2, exited=True)
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S", dirs=(DIR, DIR2))
        self.assertTrue(res.ok, res.message)

    def test_inside_requires_same_socket_dir(self):
        w, snap, node = make_world_and_snap(host=False)
        # same name, but agentree runs in a session in another socket dir: not "inside"
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S", current_dir=DIR2)
        self.assertTrue(res.ok)
        self.assertFalse(res.close)                       # treated as "attached outside kitty"
        self.assertIn("outside kitty", res.message)
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S", current_dir="/private" + DIR)
        self.assertTrue(res.ok and res.close)             # canonical compare: /tmp == /private/tmp

    def test_cached_exited_now_live_navigates_instead_of_resurrecting(self):
        w, snap, node = make_world_and_snap(state="exited", host=False)
        w.add_session("S", [(0, "main")], [(0, 0, "/r", False)], clients=1)     # live again
        w.window(12, "S")
        res = self.go(w, snap, row("session", node, "S"), current="X")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.launched, [])
        self.assertEqual(w.acts(), [("kitty", SOCK, "focus-window", "--match", "id:12")])

    def test_cached_live_now_exited_refused(self):
        w, snap, node = make_world_and_snap()
        w.s("S")["exited"] = True
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="X")
        self.assertFalse(res.ok)
        self.assertIn("has exited", res.message)
        self.assertEqual(w.launched, [])

    def test_gone_refused(self):
        w, snap, node = make_world_and_snap()
        del w.sessions[(DIR, "S")]
        res = self.go(w, snap, row("session", node, "S"))
        self.assertIn("no longer exists", res.message)

    def test_restarted_server_refused(self):
        w, snap, node = make_world_and_snap()
        w.s("S")["age"] = 3                       # expected AGE + 10 = 70 s
        res = self.go(w, snap, row("pane", node.session.tabs[1].panes[0], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("session restarted", res.message)
        self.assertEqual(w.acts(), [])
        w.s("S")["age"] = AGE + 10 - 4            # within tolerance
        self.assertTrue(self.go(w, snap, row("pane", node.session.tabs[1].panes[0], "S"), current="S").ok)

    def test_moved_socket_dir_refused(self):
        w, snap, node = make_world_and_snap()
        v = w.sessions.pop((DIR, "S"))
        w.sessions[(DIR2, "S")] = v
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S", dirs=(DIR, DIR2))
        self.assertFalse(res.ok)
        self.assertIn(f"now lives in {DIR2}", res.message)

    def test_phantom_live_listing_without_socket_is_not_ambiguous(self):
        # zellij 0.45.1 run inside S lists S live "(current)" in every socket dir it is asked
        # about; DIR2 has no socket for S, so that listing counts as exited and the jump proceeds
        w, snap, node = make_world_and_snap()
        w.phantom.add((DIR2, "S"))
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S", dirs=(DIR, DIR2))
        self.assertTrue(res.ok and res.close, res.message)
        self.assertNotIn("several socket dirs", res.message)
        self.assertIn(("zj", "S", "go-to-tab-by-id", "1"), w.acts())
        # without the socket check the same listing is ambiguous (what the user hit)
        nav = nav_for(w, current="S", dirs=(DIR, DIR2))
        nav.has_socket = lambda d, n: True
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertIn("is live in several socket dirs", res.message)

    def test_phantom_only_listing_is_exited(self):
        w, snap, node = make_world_and_snap()
        w.phantom.add((DIR2, "GHOST"))
        nav = nav_for(w, dirs=(DIR, DIR2))
        f = nav.lookup_session("GHOST")
        self.assertEqual((f.live_dir, f.exited), (None, True))
        self.assertEqual(nav.lookup_session("S").live_dir, DIR)

    def test_list_sessions_argv_carries_the_env_wrapper(self):
        w, snap, node = make_world_and_snap()
        nav_for(w, dirs=(DIR, DIR2)).lookup_session("S")
        ls = [(a, e) for a, e, _d in w.calls if "list-sessions" in a]
        self.assertEqual(len(ls), 2)
        for a, e in ls:
            self.assertEqual(a, ("/usr/bin/env", "-u", "ZELLIJ", "-u", "ZELLIJ_SESSION_NAME", "-u", "ZELLIJ_PANE_ID",
                                 ZJ, "list-sessions", "-n"))
            self.assertEqual(set(e), {"ZELLIJ_SOCKET_DIR"})
        self.assertEqual([e["ZELLIJ_SOCKET_DIR"] for _a, e in ls], [DIR, DIR2])
        with self.assertRaises(AssertionError):            # the fakes reject an unwrapped call
            w([ZJ, "list-sessions", "-n"], env={"ZELLIJ_SOCKET_DIR": DIR})

    def test_default_socket_check_is_the_sources_one(self):
        nav = RealActions(None, run_fn=World(), environ={}).navigator()
        with mock.patch.object(zsrc, "has_socket", return_value=False) as hs:
            self.assertFalse(nav.has_socket("/d", "S"))
        hs.assert_called_once_with("/d", "S")

    def test_list_sessions_failure_refuses(self):
        w, snap, node = make_world_and_snap()
        orig = w.zellij

        def broken(argv, env):
            if argv[1] == "list-sessions":
                return R(argv, 1, err="socket error")
            return orig(argv, env)

        w.zellij = broken
        res = self.go(w, snap, row("session", node, "S"))
        self.assertFalse(res.ok)
        self.assertIn("cannot list zellij sessions", res.message)


class SemanticTest(JumpBase):
    def test_worktree_tab_no_longer_holds_worktree(self):
        w, snap, node = make_world_and_snap()
        w.s("S")["panes"] = [(0, 0, "/r", False), (1, 1, "/elsewhere", False)]
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("stale", res.message)
        self.assertEqual(w.acts(), [])

    def test_worktree_tab_with_nested_worktree_pane_only(self):
        # the root worktree's tab now only holds a pane in the nested worktree x: stale for root
        w, snap, node = make_world_and_snap()
        w.s("S")["panes"] = [(0, 0, WT.path, False), (1, 1, WT.path, False)]
        res = self.go(w, snap, row("worktree", node.worktrees[0], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("stale", res.message)

    def test_agent_env_mismatch_or_dead(self):
        w, snap, node = make_world_and_snap()
        r = row("agent", node.worktrees[1].agents[0], "S")
        w.agent_env[AGENT_PID] = {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "7"}
        res = self.go(w, snap, r, current="S")
        self.assertFalse(res.ok)
        self.assertIn("no longer runs in S pane terminal_2", res.message)
        w.agent_env[AGENT_PID] = None
        self.assertIn("cannot read its environment", self.go(w, snap, r, current="S").message)
        w.agent_env[AGENT_PID] = {"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_PANE_ID": "2"}
        w.dead.add(AGENT_PID)
        self.assertIn("has exited", self.go(w, snap, r, current="S").message)
        self.assertEqual(w.acts(), [])


class OwnedSocketTest(JumpBase):
    def test_cached_host_on_unowned_socket_is_ignored(self):
        w, snap, node = make_world_and_snap()
        node = replace(node, kitty=KittyHost("unix:/var/tmp/kitty.sock-111", 1, 1, 5, "S"))
        snap = replace(snap, sessions=(node,))
        w.windows["unix:/var/tmp/kitty.sock-111"] = list(w.windows[SOCK])
        w.windows[SOCK] = []
        res = self.go(w, snap, row("session", node, "S"), current="X")
        self.assertFalse(res.ok)                                  # no owned host -> outside kitty
        self.assertFalse(any(a[0] == KT and "/var/tmp" in a[3] for a, _e, _d in w.calls))

    def test_real_owned_policy_used_by_real_actions(self):
        from agentree.sources import kitty as ksrc
        a = RealActions(None, run_fn=World(), environ={})
        self.assertIs(a.navigator().owned, ksrc.is_owned_socket)


class DryRunTest(unittest.TestCase):
    def test_dry_run_logs_without_side_effects(self):
        w, snap, node = make_world_and_snap(clients=0, host=False)
        res = nav_for(w, dry_run=True).go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertTrue(res.ok, res.message)
        self.assertFalse(res.close)
        acts = [(a, d) for a, _e, d in w.calls if (a[0] == KT and a[4] != "ls") or
                (a[0] == ZJ and a[1] == "--session" and a[4] not in ("list-tabs", "list-panes", "list-clients"))]
        self.assertEqual([a[4] for a, _ in acts], ["launch", "go-to-tab-by-id", "focus-pane-id"])
        self.assertTrue(all(d for _, d in acts))
        self.assertEqual(w.launched, [])
        self.assertEqual(res.message, "dry-run: 3 commands logged")

    def test_real_exec_dry_run_log_lines(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": d}):
            Runner(dry_run=True).act([ZJ, "--session", "S", "action", "go-to-tab-by-id", "3"],
                                     {"ZELLIJ_SOCKET_DIR": DIR})
            with open(os.path.join(d, "log")) as fh:
                line = fh.read().strip()
        self.assertIn(" DRY ", line)
        self.assertTrue(line.endswith(f"ZELLIJ_SOCKET_DIR={DIR} {ZJ} --session S action go-to-tab-by-id 3"), line)


class RealActionsTest(unittest.TestCase):
    def setUp(self):
        p1 = mock.patch.object(ex, "which", side_effect=lambda n: ZJ if n == "zellij" else KT)
        p1.start()
        self.addCleanup(p1.stop)

    def test_wiring_and_stubs(self):
        w, snap, node = make_world_and_snap()
        cfg = mock.Mock(zellij_socket_dirs=(DIR,), kitty_socket_glob="/tmp/kitty.sock-*", layouts=("agent",),
                        expanded_repo_roots=lambda: ())
        a = RealActions(cfg, run_fn=w, environ={"ZELLIJ_SESSION_NAME": "S", "ZELLIJ_SOCKET_DIR": DIR},
                        sockets=lambda: [SOCK], sleep=lambda s: None,
                        nav_overrides=fake_nav(w, clock=lambda: NOW, owned=lambda s: True))
        res = a.jump(row("pane", node.session.tabs[1].panes[0], "S"), snap)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(w.acts(), [("zj", "S", "go-to-tab-by-id", "1")])      # terminal_1: focused already
        self.assertFalse(a.jump(row("footer", None, None), snap).ok)
        for res in (a.new_agent(None, snap), a.lazygit(None, snap)):      # M4: wired, row validated
            self.assertIn("select a worktree, agent or pane", res.message)
        self.assertEqual(a.spawn_options(None, snap).layouts, ("agent",))
        self.assertFalse(a.execute(Plan(kind="bogus", confirm=None), None, progress=lambda e: None).ok)
        self.assertFalse(a.execute(Plan(kind="spawn", confirm=None, errors=("x",)), None).ok)


class PaneFocusTest(JumpBase):
    """zellij 0.45.1: focus-pane-id is a no-op for tiled panes; focus is read back from
    list-clients and approached with a bounded focus-next-pane walk."""

    def world3(self, **kw):
        """Tab 1 holds three tiled panes 1, 2, 3 (2 = the agent's); the client starts on tab 0."""
        w, snap, node = make_world_and_snap(**kw)
        w.s("S")["panes"].insert(3, (3, 1, WT.path, False))
        w.focus_tiled_noop = True
        return w, snap, node

    def zacts(self, w):
        return [a[2:] for a in w.acts() if a[0] == "zj"]

    def test_cycle_reaches_the_middle_pane(self):
        w, snap, node = self.world3()
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(res.message, "S › agent")
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1"), ("focus-pane-id", "terminal_2"),
                                         ("focus-next-pane",)])
        self.assertEqual(w.focused["S"], 2)

    def test_any_provider_agent_jumps_to_its_pane(self):
        from dataclasses import replace as _r
        for provider in ("claude", "codex"):            # the label is always "agent"
            w, snap, node = self.world3()
            a = _r(node.worktrees[1].agents[0], provider=provider, meta={}, session_id="sid")
            res = self.go(w, snap, row("agent", a, "S"), current="S")
            self.assertTrue(res.ok and res.close, res.message)
            self.assertEqual(res.message, "S › agent")
        self.assertEqual(self.zacts(w)[:2], [("go-to-tab-by-id", "1"), ("focus-pane-id", "terminal_2")])
        self.assertEqual(w.focused["S"], 2)

    def test_cycle_wraps_around(self):
        w, snap, node = self.world3()
        w.tab_focus[("S", 1)] = 3                      # tab 1 remembers pane 3: 3 -> 1 -> 2
        res = self.go(w, snap, row("pane", node.session.tabs[1].panes[1], "S"), current="S")
        self.assertTrue(res.ok, res.message)
        self.assertEqual(self.zacts(w).count(("focus-next-pane",)), 2)
        self.assertEqual(w.focused["S"], 2)

    def test_bound_exhausted(self):
        w, snap, node = self.world3()
        w.unreachable.add(2)                           # e.g. collapsed in a stack
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertEqual(res.message, "tab and window reached; could not focus pane terminal_2 "
                                      "(stacked or not reachable with focus-next-pane)")
        self.assertEqual(self.zacts(w).count(("focus-next-pane",)), 4)        # 3 tiled panes + 1

    def test_focus_failure_still_focuses_the_kitty_window(self):
        w, snap, node = self.world3()
        w.unreachable.add(2)
        res = self.go(w, snap, row("pane", node.session.tabs[1].panes[1], "S"), current="OTHER")
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertTrue(res.message.startswith("tab and window reached; could not focus pane terminal_2"))
        self.assertEqual(w.acts()[-1], ("kitty", SOCK, "focus-window", "--match", "id:5"))

    def test_tab_only_and_session_jumps_unchanged(self):
        w, snap, node = self.world3()
        res = self.go(w, snap, row("worktree", node.worktrees[1], "S"), current="S")
        self.assertTrue(res.ok and res.close, res.message)
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1")])
        res = self.go(w, snap, row("session", node, "S"), current="OTHER")
        self.assertTrue(res.ok and res.close, res.message)

    def test_tab_change_mid_walk_stops(self):
        w, snap, node = self.world3()
        w.tab_focus[("S", 1)] = 3                      # needs two steps: 3 -> 1 -> 2
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "focus-next-pane":
                w.active["S"] = 0                      # the user switched tabs meanwhile
                w.focused.pop("S", None)
            return r
        w.zellij = z
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertIn("could not focus pane terminal_2 (the client left its tab)", res.message)
        self.assertEqual(self.zacts(w).count(("focus-next-pane",)), 1)
        self.assertEqual(w.active["S"], 0)             # never followed back

    def test_layer_change_mid_walk_stops(self):
        w, snap, node = self.world3()
        w.tab_focus[("S", 1)] = 3
        w.s("S")["panes"].append((8, 1, "/tmp", False))
        w.floating.add(8)
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "focus-next-pane":
                w.focused["S"] = 8                     # a floating pane popped up and took focus
            return r
        w.zellij = z
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertIn("could not focus pane terminal_2 (floating panes are shown in that tab)", res.message)
        self.assertEqual(self.zacts(w).count(("focus-next-pane",)), 1)

    def test_each_step_revalidated(self):
        w, snap, node = self.world3()
        w.tab_focus[("S", 1)] = 3
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")
        seq = [a[4] for a, _e, _d in w.calls if a[0] == ZJ and a[1] == "--session"]
        for i, c in enumerate(seq):
            if c == "focus-next-pane":
                self.assertEqual(seq[i - 3:i], ["list-clients", "list-tabs", "list-panes"])

    def test_already_focused_needs_no_cycling(self):
        w, snap, node = self.world3()
        w.tab_focus[("S", 1)] = 2
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")
        # focus is read from list-clients right after go-to-tab-by-id: already there, no mutation
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1")])
        i = w.index(lambda a: a[0] == ZJ and a[1] == "--session" and a[4] == "go-to-tab-by-id")
        self.assertEqual(w.calls[i + 1][0][4], "list-clients")

    def test_focus_pane_id_working_needs_no_cycling(self):
        w, snap, node = make_world_and_snap()
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")
        self.assertNotIn(("focus-next-pane",), self.zacts(w))

    def test_multi_client_still_refused(self):
        w, snap, node = self.world3(clients=2)
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertFalse(res.ok)
        self.assertIn("2 clients attached", res.message)
        self.assertEqual(self.zacts(w), [])

    def test_client_count_changes_mid_walk(self):
        w, snap, node = self.world3()
        w.tab_focus[("S", 1)] = 3
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "focus-next-pane":
                w.s("S")["clients"] = 2
            return r
        w.zellij = z
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertIn("could not focus pane terminal_2 (the client changed)", res.message)
        self.assertEqual(self.zacts(w).count(("focus-next-pane",)), 1)

    HIDE = ("not hiding them — hide them (Ctrl Alt f, or Alt p w) and retry")

    def user_float(self, w, pid=8, tab=1, shown=True):
        w.s("S")["panes"].append((pid, tab, "/tmp", False))
        w.floating.add(pid)
        if shown:
            w.show_floating("S", pid)

    def test_user_floating_layer_shown_tiled_target_is_left_alone(self):
        # regression (gate M4): focus-pane-id on a tiled pane would hide the user's floating
        # layer, so neither focus-pane-id nor toggle-floating-panes is sent
        w, snap, node = self.world3()
        self.user_float(w)
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertEqual(res.message, "tab and window reached; could not focus pane terminal_2 (floating panes are "
                                      "shown in this tab (terminal_8); " + self.HIDE + ")")
        self.assertEqual(w.focus_pane_calls, [])
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1")])
        self.assertTrue(w.layer[("S", 1)])
        self.assertEqual(w._focused("S", w.s("S")), 8)

    def test_user_floating_layer_in_target_tab_reached_from_another_tab(self):
        w, snap, node = self.world3()
        self.user_float(w)
        w.active["S"] = 0                              # the client is elsewhere; tab 1 keeps its shown layer
        w.focused.pop("S")
        res = self.go(w, snap, row("pane", node.session.tabs[1].panes[1], "S"), current="OTHER")
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertIn("floating panes are shown in this tab (terminal_8); " + self.HIDE, res.message)
        self.assertEqual(w.focus_pane_calls, [])
        self.assertNotIn(("toggle-floating-panes",), self.zacts(w))
        self.assertTrue(w.layer[("S", 1)])
        self.assertEqual(w.acts()[-1], ("kitty", SOCK, "focus-window", "--match", "id:5"))

    def test_fake_models_the_layer_hiding_side_effect(self):
        # what the gate found: a plain focus-pane-id on a tiled pane from the floating layer
        w, snap, node = self.world3()
        self.user_float(w)
        w(Navigator.zj(nav_for(w), ZSession("S", "attached", 60, DIR, 1), "focus-pane-id", "terminal_2")[0],
          env={"ZELLIJ_SOCKET_DIR": DIR})
        self.assertFalse(w.layer[("S", 1)])
        self.assertEqual(w._focused("S", w.s("S")), 1)     # the tab's last tiled pane, not the target

    def test_hidden_user_floating_panes_do_not_block(self):
        w, snap, node = self.world3()
        self.user_float(w, shown=False)
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1"), ("focus-pane-id", "terminal_2"),
                                         ("focus-next-pane",)])
        self.assertFalse(w.layer.get(("S", 1)))

    def test_layer_shown_without_live_floating_panes_proceeds(self):
        w, snap, node = self.world3()
        w.layer[("S", 1)] = True                       # visible flag, but nothing floating in it
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")
        self.assertIn(("focus-pane-id", "terminal_2"), self.zacts(w))

    def popup_world(self, *extra_floating):
        w, snap, node = self.world3()
        for pid in (*extra_floating, 8):
            self.user_float(w, pid)                    # 8 = agentree's popup, focused last
        nav = nav_for(w, current="S")
        nav.env = replace(nav.env, pane_id="8")
        return w, snap, node, nav

    def unknown_visibility(self, w, mode, after=None):
        """Break the floating-layer visibility query (the list-tabs right after list-panes in
        ensure_focus / _restore_floating) — optionally only once `after` was sent."""
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if not (argv[1] == "--session" and argv[4] == "list-tabs" and len(w.calls) > 1
                    and w.calls[-2][0][1:2] == ("--session",) and w.calls[-2][0][4] == "list-panes"):
                return r
            if after is not None and (after,) not in self.zacts(w):
                return r
            if mode == "failed":
                return R(argv, 1, err="boom")
            if mode == "garbage":
                return R(argv, out="not json")
            tabs = json.loads(r.stdout)
            if mode == "missing":
                for t in tabs:
                    t.pop("are_floating_panes_visible", None)
            elif mode == "tab gone":
                tabs = [t for t in tabs if t["tab_id"] != 1]
            return R(argv, out=json.dumps(tabs))
        w.zellij = z

    def test_unknown_layer_visibility_refuses_the_tiled_focus_mutation(self):
        # a user floating layer is shown but the client reports a tiled pane: without a known
        # visibility nothing may be sent that could hide it
        for mode in ("missing", "failed", "garbage", "tab gone"):
            with self.subTest(mode):
                w, snap, node = self.world3()
                self.user_float(w)
                w.focused["S"] = 1                      # reported focus: tiled pane 1, layer shown
                self.unknown_visibility(w, mode)
                res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
                self.assertEqual((res.ok, res.close), (False, False))
                self.assertIn("could not focus pane terminal_2 (could not determine floating layer visibility)",
                              res.message)
                self.assertEqual(w.focus_pane_calls, [])
                self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1")])
                self.assertTrue(w.layer[("S", 1)])

    def test_unknown_layer_visibility_no_toggle_for_own_popup(self):
        w, snap, node, nav = self.popup_world()
        self.unknown_visibility(w, "missing")
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertIn("could not determine floating layer visibility", res.message)
        self.assertEqual(w.focus_pane_calls, [])
        self.assertNotIn(("toggle-floating-panes",), self.zacts(w))

    def test_no_floating_panes_needs_no_visibility(self):
        w, snap, node = self.world3()
        self.unknown_visibility(w, "failed")
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")

    def test_restore_skipped_when_visibility_unknown(self):
        for mode in ("missing", "failed"):
            with self.subTest(mode):
                w, snap, node, nav = self.popup_world()
                w.unreachable.add(2)
                self.unknown_visibility(w, mode, after="toggle-floating-panes")
                res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
                self.assertEqual((res.ok, res.close), (False, False))
                self.assertEqual(self.zacts(w).count(("toggle-floating-panes",)), 1)   # hidden, not re-shown
                self.assertFalse(w.layer[("S", 1)])

    def test_own_popup_is_hidden_then_walked(self):
        w, snap, node, nav = self.popup_world()
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertEqual(res.message, "S › agent")
        # tracked toggle BEFORE any focus-pane-id, then the walk
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1"), ("toggle-floating-panes",),
                                         ("focus-pane-id", "terminal_2"), ("focus-next-pane",)])
        self.assertEqual(w.focused["S"], 2)
        self.assertFalse(w.layer[("S", 1)])

    def test_own_popup_with_user_floating_panes_is_not_toggled(self):
        w, snap, node, nav = self.popup_world(9)
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertIn("could not focus pane terminal_2 (floating panes are shown in this tab (terminal_9); "
                      + self.HIDE, res.message)
        self.assertEqual(w.focus_pane_calls, [])
        self.assertNotIn(("toggle-floating-panes",), self.zacts(w))
        self.assertNotIn(("focus-next-pane",), self.zacts(w))
        self.assertTrue(w.layer[("S", 1)])

    def test_exited_user_floating_pane_does_not_block_the_toggle(self):
        w, snap, node, nav = self.popup_world()
        w.s("S")["panes"].append((9, 1, "/tmp", False))
        w.floating.add(9)
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "list-panes":
                ps = json.loads(r.stdout)
                for p in ps:
                    if p["id"] == 9:
                        p["exited"] = True
                return R(argv, out=json.dumps(ps))
            return r
        w.zellij = z
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertEqual(res.message, "S › agent")

    def test_floating_plugin_pane_blocks_the_toggle(self):
        # toggle-floating-panes hides the whole layer, plugins included; plugin_8 is not agentree's
        # terminal_8 even though the numbers match
        w, snap, node, nav = self.popup_world()
        w.s("S")["panes"].append((8, 1, None, True))
        w.floating_plugins.add(8)
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertIn("floating panes are shown in this tab (plugin_8); " + self.HIDE, res.message)
        self.assertEqual(w.focus_pane_calls, [])
        self.assertNotIn(("toggle-floating-panes",), self.zacts(w))
        self.assertNotIn(("focus-next-pane",), self.zacts(w))

    def test_foreign_focused_floating_pane_is_not_toggled(self):
        # the focused floating pane is not agentree's own (agentree runs in pane 9): report, never toggle
        w, snap, node, nav = self.popup_world()
        nav.env = replace(nav.env, pane_id="9")
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertIn("could not focus pane terminal_2 (floating panes are shown in this tab (terminal_8)",
                      res.message)
        self.assertEqual(w.focus_pane_calls, [])
        self.assertNotIn(("toggle-floating-panes",), self.zacts(w))

    def test_own_pane_of_another_session_is_not_ours(self):
        w, snap, node, nav = self.popup_world()
        nav.env = replace(nav.env, current_session="OTHER")
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertIn("floating panes are shown in this tab (terminal_8)", res.message)
        self.assertNotIn(("toggle-floating-panes",), self.zacts(w))

    def test_toggle_restored_when_the_walk_fails(self):
        w, snap, node, nav = self.popup_world()
        w.unreachable.add(2)
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertEqual((res.ok, res.close), (False, False))
        self.assertEqual(self.zacts(w).count(("toggle-floating-panes",)), 2)    # hidden, then shown again
        self.assertEqual(w._focused("S", w.s("S")), 8)                        # the popup shows the message
        self.assertTrue(w.layer[("S", 1)])

    def test_toggle_restored_when_focus_pane_id_fails(self):
        w, snap, node, nav = self.popup_world()
        w.focus_result = (1, "boom")
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertFalse(res.ok)
        self.assertIn("focus-pane-id terminal_2: boom", res.message)
        self.assertEqual(self.zacts(w).count(("toggle-floating-panes",)), 2)
        self.assertTrue(w.layer[("S", 1)])

    def test_toggle_not_restored_after_the_client_left(self):
        w, snap, node, nav = self.popup_world()
        orig = w.zellij

        def z(argv, env):
            r = orig(argv, env)
            if argv[1] == "--session" and argv[4] == "focus-next-pane":
                w.active["S"] = 0
                w.focused.pop("S", None)
            return r
        w.zellij = z
        w.tab_focus[("S", 1)] = 3
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertIn("the client left its tab", res.message)
        self.assertEqual(self.zacts(w).count(("toggle-floating-panes",)), 1)

    def test_floating_target_is_focused_directly(self):
        # a floating target (e.g. g's lazygit) gets focus-pane-id even with user floating panes shown
        w, snap, node = self.world3()
        self.user_float(w, 9)
        w.floating.add(2)
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertEqual(res.message, "S › agent")
        self.assertEqual(self.zacts(w), [("go-to-tab-by-id", "1"), ("focus-pane-id", "terminal_2")])
        self.assertTrue(w.layer[("S", 1)])

    def test_floating_target_not_walked(self):
        w, snap, node = self.world3()
        w.floating.add(2)
        orig = w.zellij

        def z(argv, env):                              # focus-pane-id fails silently even for it
            if argv[1] == "--session" and argv[4] == "focus-pane-id":
                return R(argv)
            return orig(argv, env)
        w.zellij = z
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S")
        self.assertIn("could not focus pane terminal_2 (floating pane)", res.message)
        self.assertNotIn(("focus-next-pane",), self.zacts(w))

    def test_one_overall_deadline(self):
        w, snap, node = self.world3()
        w.unreachable.add(2)
        nav = nav_for(w, current="S")
        nav.focus_deadline_s = 0.0
        res = nav.go(resolve(row("agent", node.worktrees[1].agents[0], "S"), snap))
        self.assertIn("could not focus pane terminal_2 (timed out after 0s)", res.message)
        self.assertNotIn(("focus-next-pane",), self.zacts(w))

    def test_dry_run_reads_nothing_back(self):
        w, snap, node = self.world3()
        res = self.go(w, snap, row("agent", node.worktrees[1].agents[0], "S"), current="S", dry_run=True)
        self.assertTrue(res.ok)
        self.assertEqual(res.message, "dry-run: 2 commands logged")
        self.assertNotIn("focus-next-pane", [a[4] for a, _e, _d in w.calls if a[0] == ZJ and a[1] == "--session"])

    def test_focused_pane_parsing(self):
        w, snap, node = make_world_and_snap()
        nav = nav_for(w)
        s = ZSession("S", "attached", 60, DIR, 1)
        self.assertEqual(nav.focused_pane(s), "terminal_0")
        w.s("S")["clients"] = 2
        self.assertIsNone(nav.focused_pane(s))
        w.s("S")["clients"] = 1
        w.clients_fail = True
        self.assertIsNone(nav.focused_pane(s))


if __name__ == "__main__":
    unittest.main()
