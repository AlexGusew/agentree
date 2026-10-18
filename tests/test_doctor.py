import io
import json
import os
import time
import unittest

from agentree import diagnostics as dg
from agentree import doctor as dr
from agentree.actions.exec import Result
from agentree.config import Config
from agentree.providers import claude as ccmod
from agentree.providers import opencode as oc
from agentree.providers import registry

try:
    from tests._fake_provider import CODEX, FakeProvider
except ImportError:  # pragma: no cover
    from _fake_provider import CODEX, FakeProvider  # type: ignore

HOME = "/h"


def res(argv, code=0, out="", err="", timed_out=False):
    return Result(tuple(argv), None if timed_out else code, out, err, timed_out, 0.01)


KITTY_LS = json.dumps([{"id": 1, "tabs": [{"id": 1, "windows": [{"id": 1}, {"id": 2}]}]}])
ZELLIJ_KDL = '''
keybinds {
    shared_except "locked" {
        // bind "Alt x" { Run "agentree"; }
        bind "Alt w" {
            Run "/h/bin/agentree" {
                floating true
            }
        }
    }
}
'''
DOC = json.dumps({"openapi": "3.1.0", "info": {"version": "1.0.0"},
                  "paths": {"/doc": {}, "/event": {}, "/global/event": {}}}).encode()


class FakeSys:
    """Programmable fake system for doctor.Probe."""

    def __init__(self):
        self.tools = {"zellij": "zellij 0.45.1\n", "kitty": "kitty 0.48.2 created by Kovid Goyal\n",
                      "opencode": "\x1b[H\x1b[2J1.18.27\n", "git": "git version 2.54.0 (Apple Git-157)\n"}
        self.files = {
            "/h/.config/kitty/kitty.conf": "include ./theme.conf\nallow_remote_control yes\n"
                                           "listen_on unix:/tmp/kitty.sock\n",
            "/h/.config/kitty/theme.conf": "foreground #fff\n",
            "/h/.config/zellij/config.kdl": ZELLIJ_KDL,
            "/h/.config/zellij/layouts/agent.kdl": "layout {}",
        }
        self.execs = {"/h/bin/agentree", "/h/bin/opencode-auto"}
        self.dirs = {"/h/.local/state/agentree"}
        self.sockets = ["unix:/tmp/kitty.sock-1"]
        self.kitty_ls = {"unix:/tmp/kitty.sock-1": res([], 0, KITTY_LS)}
        self.zdirs = ["/tmp/zellij-1", "/T/zellij-1"]
        self.sessions = {"/tmp/zellij-1": "a [Created 1h ago] \nold [Created 2d ago] (EXITED - attach to resurrect)\n",
                         "/T/zellij-1": "b [Created 3m ago] (current)\n"}
        self.no_socket: set[tuple[str, str]] = set()     # (socket dir, session) listed live without a socket
        self.servers = [(100, 5000, "/w"), (101, 5001, "/v")]
        self.doc: dict[int, object] = {5000: DOC, 5001: DOC}
        self.sse: dict[int, object] = {5000: "first event server.connected", 5001: "first event server.connected"}
        self.repos = ["/h/Projects/a", "/h/Projects/b"]
        self.path_agentree = "/h/bin/agentree"
        self.calls = []
        self.delay = 0.0
        self.journals: list[dict] = []
        self.alive: set[int] = set()
        self.starts: dict[int, float] = {}
        self.argvs: dict[int, tuple] = {}
        self.claude = None                                  # resolved claude argv (None: not installed)
        self.claude_agents: list = []
        self.hook_exe = "/h/bin/agentree"

    def run(self, argv, timeout=1.0, env=None, **kw):
        self.calls.append((tuple(argv), dict(env or {})))
        if self.delay:
            time.sleep(self.delay)
        name = os.path.basename(argv[0])
        if argv[1:] == ["--version"]:
            if name not in self.tools:
                return res(argv, 127, "", "not found")
            v = self.tools[name]
            return v if isinstance(v, Result) else res(argv, 0, v)
        if name == "kitty":
            return self.kitty_ls[argv[3]]
        if name == "env" and "list-sessions" in argv:     # zellij list-sessions via env -u
            v = self.sessions[env["ZELLIJ_SOCKET_DIR"]]
            return v if isinstance(v, Result) else res(argv, 0, v)
        raise AssertionError(argv)

    def which(self, name):
        return f"/bin/{name}" if name in self.tools else None

    def http_get(self, port, target, timeout, max_bytes=0):
        assert target == "/doc"
        v = self.doc[port]
        if isinstance(v, Exception):
            raise v
        return v

    def sse_probe(self, port, cwd, timeout):
        v = self.sse[port]
        if isinstance(v, Exception):
            raise v
        return v

    def probe(self):
        """The generic (provider-neutral) probe."""
        return dg.Probe(
            run=self.run, which=self.which, path_which=lambda n, p: self.path_agentree,
            kitty_sockets=lambda g: list(self.sockets), is_owned_socket=lambda p: "unix:" + p in self.sockets, zellij_dirs=lambda d: list(d or self.zdirs),
            has_socket=lambda d, n: (d, n) not in self.no_socket,
            discover_repos=lambda g: list(self.repos), read_text=self.files.get,
            glob=lambda pat: sorted(p for p in self.files if __import__("fnmatch").fnmatch(p, pat)),
            exists=lambda p: p in self.files or p in self.dirs or p in self.execs or p in ("/h", "/"),
            is_exec=lambda p: p in self.execs, writable_dir=lambda p: p in self.dirs or p == "/h",
            state_dir=lambda: "/h/.local/state/agentree", list_journals=lambda: list(self.journals),
            pid_alive=lambda pid: pid in self.alive, start_time=self.starts.get, proc_argv=self.argvs.get, home=HOME, environ={"PATH": "/h/bin:/usr/bin"},
            python_version=(3, 14, 7))

    def providers(self):
        """The real opencode and claude providers (registry order) with their provider-local
        doctor access faked; the lambdas read this FakeSys at check time."""
        out = []
        for s in registry.specs():
            if s.id == "opencode":
                local = oc.DoctorProbe(servers=lambda t: list(self.servers),
                                       http_get=lambda *a, **kw: self.http_get(*a, **kw),
                                       sse_probe=lambda *a, **kw: self.sse_probe(*a, **kw))
                out.append(oc.OpencodeProvider(s, doctor_probe=local))
            elif s.id == "claude":
                local = ccmod.DoctorProbe(resolve=lambda cfg: self.claude, claude_dir=lambda: "/h/.claude",
                                          settings_path=lambda: "/h/.local/state/agentree/claude/settings.json",
                                          hook_argv0=lambda: self.hook_exe,
                                          agents=lambda t: list(self.claude_agents))
                out.append(ccmod.ClaudeProvider(s, doctor_probe=local))
            else:
                out.append(registry.get(s.id))
        return out


def by(checks, section=None, level=None):
    return [c.text for c in checks if (section is None or c.section == section)
            and (level is None or c.level == level)]


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def collect(self, cfg=None, **kw):
        kw.setdefault("providers", self.fs.providers())
        return dr.collect(cfg or Config(), self.fs.probe(), **kw)

    def run_doctor(self, cfg=None, **kw):
        kw.setdefault("out", io.StringIO())
        return dr.run_doctor(cfg or Config(), probe=self.fs.probe(), providers=self.fs.providers(), **kw)

    def test_all_good_report(self):
        out = io.StringIO()
        code = self.run_doctor(out=out)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        for needle in ("✓ OK    zellij 0.45.1", "kitty 0.48.2", "opencode 1.18.27", "git 2.54.0", "python 3.14.7",
                       "! WARN  g (lazygit) disabled; install lazygit to enable", "\nversions\n", "\nkitty\n",
                       "\nzellij\n", "\nopencode\n", "\nconfig\n", "0 failure(s)"):
            self.assertIn(needle, text)
        self.assertNotIn("✗", text)
        text.encode("ascii", "ignore")  # plain text
        order = dr.sections(self.fs.providers())
        self.assertEqual(order, ("versions", "kitty", "zellij", "opencode", "claude", "config"))
        sections = [ln for ln in text.splitlines() if ln in order]
        self.assertEqual(sections, list(order))
        versions = text.split("\nversions\n")[1].split("\n\n")[0]
        self.assertLess(versions.index("kitty 0.48.2"), versions.index("opencode 1.18.27"))   # unchanged order
        self.assertLess(versions.index("opencode 1.18.27"), versions.index("git 2.54.0"))

    def test_claude_absent_is_not_a_failure(self):
        out = io.StringIO()
        code = self.run_doctor(out=out)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        c = self.collect()
        self.assertEqual(by(c, "claude", "warn"), ["claude_cmd claude: not found (Claude Code agents disabled; optional)"])
        self.assertEqual(by(c, "claude", "ok"), [
            "claude dir ~/.claude not found (Claude Code not used yet)",
            "hooks settings dir ~/.local/state/agentree/claude will be created",
            "hook command runs ~/bin/agentree",
            "no running claude agents"])
        self.assertEqual(by(c, "claude", "fail"), [])
        self.assertNotIn("claude --version", " ".join(" ".join(a) for a, _ in self.fs.calls))   # never runs claude

    def test_claude_present(self):
        from agentree.types import Agent
        self.fs.claude = ("/h/.local/bin/claude",)
        self.fs.dirs |= {"/h/.claude", "/h/.local/state/agentree/claude"}

        def ag(pid, src):
            return Agent(pid=pid, cwd="/w", session_name=None, pane_id=None, provider="claude", status_source=src)
        self.fs.claude_agents = [ag(1, "hooks"), ag(2, "transcript"), ag(3, None)]
        self.fs.hook_exe = "/h/missing/agentree"
        c = self.collect()
        oks = by(c, "claude", "ok")
        self.assertIn("claude_cmd claude -> ~/.local/bin/claude", oks)
        self.assertIn("claude dir ~/.claude", oks)
        self.assertIn("hooks settings dir ~/.local/state/agentree/claude writable", oks)
        self.assertIn("3 running claude agent(s), 1 with hook status (the others: approximate status from "
                      "Claude's registry / transcript)", oks)
        self.assertEqual(by(c, "claude", "warn"),
                         ["hook command ~/missing/agentree is not executable (claude status falls back to the transcript)"])

    def test_claude_unwritable_hooks_dir_and_crash(self):
        self.fs.dirs = {"/h/.local/state/agentree"}
        self.fs.files["/h/.local/state/agentree/claude"] = ""          # exists, not a writable dir
        self.fs.claude_agents = None                               # list(None) -> TypeError in the probe
        c = self.collect()
        self.assertIn("hooks settings dir ~/.local/state/agentree/claude not a writable directory", by(c, "claude", "fail"))
        self.assertTrue(any(t.startswith("cannot list claude processes") for t in by(c, "claude", "warn")))

    def test_hook_argv0(self):
        from unittest import mock
        with mock.patch.object(ccmod, "hook_command", return_value="PYTHONPATH=/src /usr/bin/python3 -m agentree claude-hook"):
            self.assertEqual(ccmod._hook_argv0(), "/usr/bin/python3")
        with mock.patch.object(ccmod, "hook_command", return_value="/h/bin/agentree claude-hook"):
            self.assertEqual(ccmod._hook_argv0(), "/h/bin/agentree")

    def test_versions(self):
        self.fs.tools["zellij"] = "zellij 0.44.2\n"
        self.fs.tools["lazygit"] = "commit=abc123, build date=2024, build source=x, version=0.44.1, os=darwin\n"
        del self.fs.tools["git"]
        c = self.collect()
        self.assertIn("zellij 0.44.2 (need >= 0.45)", by(c, "versions", "fail"))
        self.assertIn("git: not found", by(c, "versions", "fail"))
        self.assertIn("lazygit 0.44.1", by(c, "versions", "ok"))
        self.assertIn("opencode 1.18.27", by(c, "versions", "ok"))
        self.assertEqual(self.run_doctor(), 1)

    def test_version_failures(self):
        self.fs.tools["opencode"] = res(["opencode"], 1, "", "boom")
        self.fs.tools["kitty"] = res(["kitty"], None, "", "", timed_out=True)
        c = self.collect()
        self.assertIn("opencode: `opencode --version` failed: boom", by(c, "versions", "warn"))
        self.assertIn("kitty: `kitty --version` failed: timed out", by(c, "versions", "fail"))

    def test_parse_version(self):
        self.assertEqual(dr.parse_version("\x1b[H\x1b[2J1.18.27"), (1, 18, 27))
        self.assertEqual(dr.parse_version("git version 2.54.0 (Apple Git-157)"), (2, 54, 0))
        self.assertEqual(dr.parse_version("version=0.44.1, commit=1.2"), (0, 44, 1))
        self.assertIsNone(dr.parse_version("nothing"))

    def test_kitty(self):
        c = self.collect()
        self.assertIn("unix:/tmp/kitty.sock-1: kitty @ ls ok (1 OS window(s), 2 window(s))", by(c, "kitty", "ok"))
        self.assertIn("kitty.conf: allow_remote_control yes", by(c, "kitty", "ok"))
        self.assertIn("kitty.conf: listen_on unix:/tmp/kitty.sock", by(c, "kitty", "ok"))
        self.assertEqual(by(c, "kitty", "warn"), [])
        self.fs.files["/tmp/kitty.sock-1"] = ""
        self.fs.files["/tmp/kitty.sock-99"] = ""          # dead kitty: not owned
        self.assertEqual(by(self.collect(), "kitty", "warn"),
                         ["ignoring /tmp/kitty.sock-99: not an owned live kitty socket (stale or foreign)"])
        del self.fs.files["/tmp/kitty.sock-1"], self.fs.files["/tmp/kitty.sock-99"]
        # ls failure, settings coming from an include, remote control disabled
        self.fs.sockets.append("unix:/tmp/kitty.sock-2")
        self.fs.kitty_ls["unix:/tmp/kitty.sock-2"] = res([], 1, "", "Error: permission denied")
        self.fs.files["/h/.config/kitty/kitty.conf"] = "include theme.conf\nglobinclude extra/*.conf\n" \
                                                       "allow_remote_control no\n"
        self.fs.files["/h/.config/kitty/extra/rc.conf"] = "listen_on unix:/var/run/k.sock\n"
        c = self.collect()
        fails = by(c, "kitty", "fail")
        self.assertIn("unix:/tmp/kitty.sock-2: kitty @ ls failed: Error: permission denied", fails)
        self.assertIn("kitty.conf: allow_remote_control no (need yes or socket-only)", fails)
        self.assertIn("kitty.conf: listen_on unix:/var/run/k.sock does not match kitty_socket_glob /tmp/kitty.sock-*",
                      by(c, "kitty", "warn"))

    def test_kitty_missing_conf_and_sockets(self):
        self.fs.sockets = []
        del self.fs.files["/h/.config/kitty/kitty.conf"]
        c = self.collect()
        self.assertTrue(any("no owned kitty sockets" in t for t in by(c, "kitty", "warn")))
        self.assertIn("kitty.conf not found at ~/.config/kitty/kitty.conf", by(c, "kitty", "fail"))
        self.fs.files["/h/.config/kitty/kitty.conf"] = "# nothing\n"
        c = self.collect()
        fails = by(c, "kitty", "fail")
        self.assertTrue(any("allow_remote_control not set" in t for t in fails))
        self.assertTrue(any("listen_on not set" in t for t in fails))

    def test_kitty_conf_settings_include_cycle(self):
        files = {"/k/kitty.conf": "include a.conf\nlisten_on unix:/tmp/kitty.sock\n",
                 "/k/a.conf": "include kitty.conf\nallow_remote_control socket-only\ninclude missing.conf\n"}
        vals, read, problems = dr.kitty_conf_settings("/k/kitty.conf", files.get, lambda p: [])
        self.assertEqual(vals, {"allow_remote_control": "socket-only", "listen_on": "unix:/tmp/kitty.sock"})
        self.assertEqual(read, ["/k/kitty.conf", "/k/a.conf"])
        self.assertEqual(problems, ["cannot read /k/missing.conf"])

    def test_listen_on_matches(self):
        self.assertTrue(dr.listen_on_matches("unix:/tmp/kitty.sock", "/tmp/kitty.sock-*"))
        self.assertTrue(dr.listen_on_matches("unix:/tmp/kitty.sock-{kitty_pid}", "/tmp/kitty.sock-*"))
        self.assertFalse(dr.listen_on_matches("unix:@kitty", "/tmp/kitty.sock-*"))
        self.assertFalse(dr.listen_on_matches("tcp:localhost:1234", "/tmp/kitty.sock-*"))

    def test_zellij(self):
        c = self.collect()
        ok = by(c, "zellij", "ok")
        self.assertIn("socket dir /tmp/zellij-1: 1 live session(s): a", ok)
        self.assertIn("socket dir /T/zellij-1: 1 live session(s): b", ok)
        self.assertIn("keybind Alt w runs ~/bin/agentree", ok)
        self.assertIn("layouts: agent (~/.config/zellij/layouts/agent.kdl), default (built-in)", ok)
        ls = [(a, e) for a, e in self.fs.calls if a[-2:] == ("list-sessions", "-n")]
        self.assertEqual(sorted(e["ZELLIJ_SOCKET_DIR"] for _a, e in ls), ["/T/zellij-1", "/tmp/zellij-1"])
        for a, e in ls:   # the caller's zellij client env is removed (env -u), never passed on
            self.assertEqual(a[:7], ("/usr/bin/env", "-u", "ZELLIJ", "-u", "ZELLIJ_SESSION_NAME", "-u", "ZELLIJ_PANE_ID"))
            self.assertEqual(a[7:], ("/bin/zellij", "list-sessions", "-n"))
            self.assertFalse({"ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"} & set(e))

    def test_zellij_listed_live_without_socket_is_not_live(self):
        # bug: list-sessions in a dir that does not host the session still prints it live
        # ("(current)") with the caller's zellij env; no socket there -> not live, no 2-dir warning
        self.fs.sessions["/T/zellij-1"] = "a [Created 3m ago] (current)\nb [Created 3m ago]\n"
        self.fs.no_socket = {("/T/zellij-1", "a")}
        c = self.collect()
        self.assertIn("socket dir /T/zellij-1: 1 live session(s): b", by(c, "zellij", "ok"))
        self.assertFalse([t for t in by(c, "zellij") if "socket dirs" in t])

    def test_zellij_duplicates_and_failures(self):
        self.fs.sessions["/T/zellij-1"] = "a [Created 3m ago] \n"
        self.fs.zdirs.append("/x/zellij-1")
        self.fs.sessions["/x/zellij-1"] = res([], 1, "", "permission denied")
        c = self.collect()
        warns = by(c, "zellij", "warn")
        self.assertTrue(any("session 'a' is live in 2 socket dirs" in t for t in warns), warns)
        self.assertIn("socket dir /x/zellij-1: list-sessions failed: permission denied", warns)
        self.fs.zdirs = ["/e"]
        self.fs.sessions["/e"] = res([], 1, "No active zellij sessions found.\n", "")
        c = self.collect()
        self.assertIn("socket dir /e: 0 live session(s)", by(c, "zellij", "ok"))
        self.fs.zdirs = []
        self.assertTrue(any("no zellij socket dirs" in t for t in by(self.collect(), "zellij", "warn")))

    def test_zellij_keybind_and_layouts_missing(self):
        self.fs.files["/h/.config/zellij/config.kdl"] = 'keybinds {\n  // bind "Alt w" { Run "agentree"; }\n}\n'
        cfg = Config(layouts=("agent", "nope"), tab_layout="ghost")
        c = self.collect(cfg)
        warns = by(c, "zellij", "warn")
        self.assertTrue(any(t.startswith("no keybind runs agentree") for t in warns), warns)
        self.assertTrue(any("layout 'nope' not found" in t for t in warns))
        self.assertTrue(any("layout 'ghost' not found" in t for t in by(c, "zellij", "fail")))

    def test_find_agentree_keybinds(self):
        kdl = 'bind "Alt w" "Alt W" { Run "agentree" "--x" { floating true; } }\nbind "F2" { Run "nvim"; }\n' \
              'bind "F3" { Run "/opt/agentree-tools/other"; }\n/* bind "F4" { Run "agentree"; } */'
        self.assertEqual(dr.find_agentree_keybinds(kdl), [("Alt w, Alt W", "agentree")])
        self.assertEqual(dr.find_agentree_keybinds(ZELLIJ_KDL), [("Alt w", "/h/bin/agentree")])

    def test_opencode(self):
        c = self.collect()
        ok = by(c, "opencode", "ok")
        self.assertIn("opencode_cmd opencode", ok)
        self.assertIn("pid 100 :5000: /doc ok (OpenAPI 3.1.0, 3 paths)", ok)
        self.assertIn("pid 101 :5001: SSE /event ok (first event server.connected)", ok)
        self.fs.doc[5001] = oc.ApiDown("deadline exceeded")
        self.fs.sse[5000] = oc.ApiDown("no data within deadline")
        self.fs.servers.append((102, None, "/z"))
        c = self.collect()
        fails = by(c, "opencode", "fail")
        self.assertIn("pid 101 :5001: /doc unreachable: deadline exceeded", fails)
        self.assertIn("pid 100 :5000: SSE /event failed: no data within deadline", fails)
        self.assertIn("pid 102: no listening port (status unavailable)", by(c, "opencode", "warn"))

    def test_opencode_attach_clients_share_server_probe(self):
        AT = oc.parse_attach
        self.fs.servers = [
            (100, 5000, "/w"),
            (102, 5000, "/w", AT(("opencode", "attach", "http://127.0.0.1:5000/", "--session", "ses_abc", "--dir", "/w"))),
            (103, 5000, "/w", AT(("opencode", "attach", "http://localhost:5000", "-c"))),
            (104, None, "/w", AT(("opencode", "attach", "http://build.example:4096", "-s", "ses_r"))),
            (105, 5001, "/v", AT(("opencode", "attach", "http://[::1]:5001/", "-s", "ses_v"))),
        ]
        probed = []
        get, sse = self.fs.http_get, self.fs.sse_probe
        self.fs.http_get = lambda port, *a, **kw: (probed.append(("doc", port)), get(port, *a, **kw))[1]
        self.fs.sse_probe = lambda port, *a, **kw: (probed.append(("sse", port)), sse(port, *a, **kw))[1]
        c = self.collect()
        ok, warns = by(c, "opencode", "ok"), by(c, "opencode", "warn")
        self.assertIn("pid 102: attach → :5000 (session ses_abc)", ok)
        self.assertIn("pid 103: attach → :5000", ok)
        self.assertIn("pid 105: attach → :5001 (session ses_v)", ok)
        self.assertIn("pid 100 :5000: /doc ok (OpenAPI 3.1.0, 3 paths)", ok)
        self.assertIn("pid 105 attach :5001: SSE /event ok (first event server.connected)", ok)  # no local server: probed once
        self.assertTrue(any(t.startswith("pid 104: attach http://build.example:4096: attach to non-local server")
                            for t in warns), warns)
        self.assertFalse([t for t in by(c, "opencode") if "no listening port" in t])
        self.assertEqual(sorted(probed), [("doc", 5000), ("doc", 5001), ("sse", 5000), ("sse", 5001)])
        # the non-local client has no stream: the popup keeps polling
        self.assertIn("live status: event streams on 2/3 server(s); the popup polls every 2 s while any stream is down", warns)
        self.fs.servers = self.fs.servers[:3]
        c = self.collect()
        self.assertIn("live status: event streams on 1/1 server(s); the popup refreshes agents on events, "
                      "everything else every 10 s", by(c, "opencode", "ok"))

    def test_opencode_none_and_missing_cmd(self):
        self.fs.servers = []
        self.fs.execs.discard("/h/bin/opencode-auto")
        c = self.collect(Config(opencode_cmd="~/bin/opencode-auto"))
        self.assertIn("no running opencode servers", by(c, "opencode", "ok"))
        self.assertTrue(any("not an executable" in t for t in by(c, "opencode", "warn")))
        self.fs.doc[5000] = json.dumps({"openapi": "3.1.0", "paths": {"/doc": {}}}).encode()
        self.fs.servers = [(100, 5000, "/w")]
        c = self.collect()
        self.assertTrue(any("lists no /event endpoint" in t for t in by(c, "opencode", "warn")))

    def test_config(self):
        cfg = Config(path="/h/.config/agentree/config.toml", warnings=("unknown config key 'x'",))
        c = self.collect(cfg)
        ok = by(c, "config", "ok")
        self.assertIn("config ~/.config/agentree/config.toml", ok)
        self.assertIn("config: unknown config key 'x'", by(c, "config", "warn"))
        self.assertIn("repo_roots ~/Projects/* -> 2 repo(s)", ok)
        self.assertIn("worktree template {repo}/.worktrees/{slug} (e.g. ~/Projects/a/.worktrees/<slug>)", ok)
        self.assertIn("state dir ~/.local/state/agentree writable", ok)
        self.assertIn("~/bin/agentree installed", ok)
        self.assertIn("agentree on PATH", ok)

    def test_config_problems(self):
        self.fs.repos = []
        self.fs.dirs.clear()
        self.fs.execs.discard("/h/bin/agentree")
        self.fs.path_agentree = None
        c = self.collect(Config(worktree_template="{repo}/{bogus}/{slug}"))
        warns = by(c, "config", "warn")
        self.assertIn("repo_roots ~/Projects/* -> no git repositories", warns)
        self.assertTrue(any(t.startswith("worktree template") for t in by(c, "config", "fail")))
        self.assertIn("state dir ~/.local/state/agentree (will be created)", by(c, "config", "ok"))
        self.assertIn("~/bin/agentree not installed (python3 scripts/build.py)", warns)
        self.assertTrue(any("not on PATH" in t for t in warns))
        self.fs.path_agentree = "/h/.local/bin/agentree"          # pipx / uv tool install
        c = self.collect()
        self.assertIn("agentree on PATH is ~/.local/bin/agentree", by(c, "config", "ok"))
        self.assertFalse(any("~/bin/agentree" in t for t in by(c, "config", "warn")))
        self.fs.path_agentree = "/usr/local/bin/agentree"
        self.fs.execs.add("/h/bin/agentree")
        self.assertIn("agentree on PATH is /usr/local/bin/agentree (not ~/bin/agentree)", by(self.collect(), "config", "warn"))

    def test_journals(self):
        self.assertIn("no leftover spawn journals", by(self.collect(), "config", "ok"))
        jd = "/h/.local/state/agentree/journal"
        self.fs.journals = [
            {"run_id": "spawn-1", "path": f"{jd}/spawn-1.json",
             "data": {"run_id": "spawn-1", "branch": "agent/x", "worktree": "/h/p/r/.worktrees/x", "pid": 4242}},
            {"run_id": "spawn-2", "path": f"{jd}/spawn-2.json", "data": None},
            {"run_id": "spawn-3", "path": f"{jd}/spawn-3.json", "data": {"pid": 777, "branch": "agent/y"}},
        ]
        self.fs.alive = {777}
        self.fs.argvs = {777: ("/opt/homebrew/bin/python3", "/h/bin/agentree")}
        c = self.collect()
        warns = by(c, "config", "warn")
        self.assertIn("interrupted spawn; inspect and clean up: ~/.local/state/agentree/journal/spawn-1.json "
                      "(branch agent/x, worktree ~/p/r/.worktrees/x)", warns)
        self.assertIn("interrupted spawn; inspect and clean up: ~/.local/state/agentree/journal/spawn-2.json", warns)
        self.assertIn("spawn in progress (pid 777): ~/.local/state/agentree/journal/spawn-3.json", by(c, "config", "ok"))
        self.assertEqual(self.run_doctor(), 0)   # WARN only

    def test_journal_pid_identity(self):
        jd = "/h/.local/state/agentree/journal"

        def state_of(data):
            self.fs.journals = [{"run_id": "s", "path": f"{jd}/s.json", "data": data}]
            c = self.collect()
            return "running" if by(c, "config", "ok") and any("in progress" in t for t in by(c, "config", "ok")) \
                else "interrupted" if any("interrupted spawn" in t for t in by(c, "config", "warn")) else "?"
        self.fs.alive = {900}
        # no recorded start time: alive + a agentree argv
        self.fs.argvs = {900: ("python3", "-m", "agentree", "spawn", "r", "b")}
        self.assertEqual(state_of({"pid": 900}), "running")
        self.fs.argvs = {900: ("/usr/bin/vim",)}                     # pid reused by something else
        self.assertEqual(state_of({"pid": 900}), "interrupted")
        self.fs.argvs = {}                                           # argv unreadable
        self.assertEqual(state_of({"pid": 900}), "interrupted")
        # recorded start time must match (argv ignored then)
        self.fs.argvs = {900: ("/h/bin/agentree",)}
        self.fs.starts = {900: 1700000000.25}
        self.assertEqual(state_of({"pid": 900, "start": 1700000000.25}), "running")
        self.assertEqual(state_of({"pid": 900, "start": 1600000000.0}), "interrupted")   # pid reused
        self.fs.starts = {}
        self.assertEqual(state_of({"pid": 900, "start": 1700000000.25}), "interrupted")
        self.fs.alive = set()
        self.fs.starts = {900: 1700000000.25}
        self.assertEqual(state_of({"pid": 900, "start": 1700000000.25}), "interrupted")  # dead
        self.assertEqual(state_of({"pid": True}), "interrupted")

    def test_list_journals_default(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": d}):
            self.assertEqual(dr._list_journals(), [])
            os.makedirs(os.path.join(d, "journal"))
            with open(os.path.join(d, "journal", "spawn-9.json"), "w") as fh:
                json.dump({"branch": "agent/z", "pid": 1}, fh)
            with open(os.path.join(d, "journal", "notes.txt"), "w") as fh:
                fh.write("x")
            got = dr._list_journals()
            self.assertEqual([(j["run_id"], j["path"]) for j in got],
                             [("spawn-9", os.path.join(d, "journal", "spawn-9.json"))])
            self.assertEqual(got[0]["data"]["branch"], "agent/z")
            # fallback glob when state.list_journals is unavailable
            import agentree.state as st
            with mock.patch.object(st, "list_journals", None):
                got2 = dr._list_journals()
            self.assertEqual([j["run_id"] for j in got2], ["spawn-9"])
            self.assertEqual(got2[0]["data"]["branch"], "agent/z")

    def test_deadline_and_crash(self):
        def hang(*a, **k):
            time.sleep(3)
        p = self.fs.probe()
        provs = self.fs.providers()
        next(x for x in provs if x.id == "opencode").doctor_probe.servers = hang

        def crash(_):
            raise RuntimeError("kaput")
        p.kitty_sockets = crash
        t0 = time.monotonic()
        c = dr.collect(Config(), p, deadline_s=0.5, providers=provs)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertIn("opencode check timed out", by(c, "opencode", "fail"))
        self.assertIn("kitty check crashed: RuntimeError: kaput", by(c, "kitty", "fail"))

    def test_parallel_fast(self):
        self.fs.delay = 0.2       # every subprocess takes 0.2 s; ~11 of them
        t0 = time.monotonic()
        code = self.run_doctor()
        self.assertLess(time.monotonic() - t0, 0.8)
        self.assertEqual(code, 0)


class ProviderDoctorTest(unittest.TestCase):
    """Doctor knows no provider: sections, version lines and checks come from the providers."""

    def setUp(self):
        self.fs = FakeSys()

    def test_fake_third_provider_gets_its_own_section(self):
        fake = FakeProvider()
        provs = [*self.fs.providers(), fake]
        out = io.StringIO()
        code = dr.run_doctor(Config(), out=out, probe=self.fs.probe(), providers=provs)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        self.assertEqual(dr.sections(provs), ("versions", "kitty", "zellij", "opencode", "claude", "codex", "config"))
        self.assertEqual([ln for ln in text.splitlines() if ln in dr.sections(provs)], list(dr.sections(provs)))
        self.assertIn("\ncodex\n  ! WARN  codex_cmd not found\n", text)
        versions = text.split("\nversions\n")[1].split("\n\n")[0].splitlines()
        names = [ln.split()[2] for ln in versions]
        self.assertEqual(names[:5], ["zellij", "kitty", "opencode", "codex", "git"])   # after kitty, registry order
        self.assertEqual([c[0] for c in fake.calls], ["doctor_jobs"])

    def test_only_a_fake_provider(self):
        c = dr.collect(Config(), self.fs.probe(), providers=[FakeProvider()])
        self.assertEqual(list(dict.fromkeys(x.section for x in c)), ["versions", "kitty", "zellij", "codex", "config"])
        self.assertFalse([x for x in c if x.section in ("opencode", "claude")])
        self.assertNotIn("opencode", " ".join(" ".join(a) for a, _ in self.fs.calls))

    def test_doctor_jobs_crash_and_job_crash_are_fail_lines(self):
        class Broken(FakeProvider):
            def doctor_jobs(self, cfg, probe):
                raise RuntimeError("no jobs")

        class BadJob(FakeProvider):
            def doctor_jobs(self, cfg, probe):
                from agentree.providers.base import DoctorJob
                return (DoctorJob(self.id, lambda: 1 / 0),)
        import dataclasses
        aider = dataclasses.replace(CODEX, id="aider", command_key="aider_cmd", default_command="aider",
                                    internal_commands=())
        c = dr.collect(Config(), self.fs.probe(), providers=[Broken(), BadJob(aider)])
        self.assertEqual(by(c, "codex", "fail"), ["codex checks crashed: RuntimeError: no jobs"])
        self.assertEqual(by(c, "aider", "fail"), ["aider check crashed: ZeroDivisionError: division by zero"])
        self.assertTrue(by(c, "kitty", "ok"))                      # the rest of the report is intact
        self.assertEqual(dr.run_doctor(Config(), out=io.StringIO(), probe=self.fs.probe(), providers=[Broken()]), 1)

    def test_unloadable_registry_provider_is_a_fail_line(self):
        import dataclasses
        broken = dataclasses.replace(CODEX, module="tests._no_such_provider_module")
        with registry.override(specs=(broken,)):
            provs, bad = dr.load_providers()
        self.assertEqual(provs, [])
        self.assertEqual([(b.section, b.level) for b in bad], [("codex", "fail")])
        self.assertIn("cannot load provider tests._no_such_provider_module", bad[0].text)
        with registry.override(specs=(broken,)):
            c = dr.collect(Config(), self.fs.probe())
        self.assertIn(bad[0].text, by(c, "codex", "fail"))

    def test_default_providers_come_from_the_registry(self):
        fake = FakeProvider()
        with registry.override(specs=(CODEX,), providers={"codex": fake}):
            c = dr.collect(Config(), self.fs.probe())
        self.assertIn("codex 9.9.9", by(c, "versions", "ok"))
        self.assertEqual([x[0] for x in fake.calls], ["doctor_jobs"])


if __name__ == "__main__":
    unittest.main()
