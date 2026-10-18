"""providers.claude: session resolution, hook / registry / transcript status, titles, fetch.

Every test runs against a temp CLAUDE_CONFIG_DIR and AGENTREE_STATE_DIR with mocked process
inspection: the real ~/.claude, ~/.local/state/agentree and running processes are never touched."""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from agentree.providers import claude as ccmod
from agentree.providers import claude as cs
from agentree.sources import proc as procmod

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "claude")
NOW = 1_800_000_000.0
SID = "7f3c2a10-1b2c-4d5e-8f90-a1b2c3d4e5f6"


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def user(text, t, **kw):
    return {"type": "user", "timestamp": iso(t), "isSidechain": False, "sessionId": kw.pop("sid", SID),
            "message": {"role": "user", "content": text}, **kw}


def tool_result(t, content="ok"):
    return {"type": "user", "timestamp": iso(t), "isSidechain": False, "toolUseResult": {},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": content}]}}


def assistant(t, text=None, tool=False, stop: str | None = "end_turn", usage=None, model="claude-sonnet-4-5-20250929",
              sidechain=False):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "x", "name": "Bash", "input": {}})
    msg = {"role": "assistant", "model": model, "content": content, "stop_reason": stop}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "timestamp": iso(t), "isSidechain": sidechain, "sessionId": SID, "message": msg}


class _Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cdir = os.path.join(self.tmp, "claude")
        self.sdir = os.path.join(self.tmp, "state")
        os.makedirs(self.cdir)
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cdir, "AGENTREE_STATE_DIR": self.sdir})
        env.start()
        self.addCleanup(env.stop)
        cs._cache.clear()
        self.work = os.path.realpath(os.path.join(self.tmp, "work"))
        os.makedirs(self.work)

    def transcript(self, records, sid=SID, cwd=None, mtime=None, root=None, raw_tail=""):
        d = os.path.join(root or self.cdir, "projects", cs.encode_project(cwd or self.work))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{sid}.jsonl")
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
            fh.write(raw_tail)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def hook(self, sid=SID, **kw):
        rec = {"v": 1, "session_id": sid, "status": "busy", "event": "UserPromptSubmit", "ts": NOW - 10,
               "since": NOW - 30, "cwd": self.work, "transcript_path": None, "pid": 100,
               "zellij_session": None, "zellij_pane": None, "message": None, "last_message": None}
        rec.update(kw)
        os.makedirs(ccmod.status_dir(), exist_ok=True)
        with open(ccmod.status_path(sid), "w") as fh:
            json.dump(rec, fh)
        return rec

    def registry(self, proc_pid, root=None, **data):
        d = os.path.join(root or self.cdir, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{proc_pid}.json"), "w") as fh:
            json.dump(data, fh)

    def fetch(self, procs, *, starts=None, cwds=None, pids=None, now=NOW):
        """procs: {pid: (argv, env)}; argv None = process gone."""
        rows = [(pid, 1, " ".join(argv or ())) for pid, (argv, _e) in procs.items()]

        def args_env(pid):
            argv, env = procs.get(pid, (None, None))
            return (tuple(argv), dict(env or {})) if argv else None
        starts = starts if starts is not None else {p: NOW - 3600 for p in procs}
        cwds = cwds if cwds is not None else {p: self.work for p in procs}
        with mock.patch.object(procmod, "list_procs", return_value=rows), \
                mock.patch.object(procmod, "proc_args_env", side_effect=args_env), \
                mock.patch.object(procmod, "proc_cwd", side_effect=lambda ps, timeout=1.0: {p: cwds[p] for p in ps if p in cwds}), \
                mock.patch.object(procmod, "start_times", side_effect=lambda ps: {p: starts[p] for p in ps if p in starts}), \
                mock.patch.object(procmod, "listening_ports", side_effect=AssertionError("no lsof for claude")):
            agents, warns = cs.fetch(timeout=0.5, pids=pids, now=now)
        return {a.pid: a for a in agents}, warns


# --------------------------------------------------------------------------- pure helpers

class HelpersTest(unittest.TestCase):
    def test_encode_project(self):
        self.assertEqual(cs.encode_project("/Users/me/Projects/web-app/.worktrees/x_y"),
                         "-Users-me-Projects-web-app--worktrees-x-y")
        self.assertEqual(cs.encode_project("/tmp/caf\u00e9 bar"), "-tmp-caf--bar")

    def test_project_dirs_long_paths_match_by_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            cwd = "/very/" + "long-segment/" * 30
            enc = cs.encode_project(cwd)
            self.assertGreater(len(enc), cs.MAX_ENCODED)
            d = os.path.join(root, "projects", enc[: cs.MAX_ENCODED] + "-h4sh")
            os.makedirs(d)
            self.assertEqual(cs.project_dirs(cwd, root), [d])
            short = os.path.join(root, "projects", cs.encode_project("/a/b"))
            os.makedirs(short)
            self.assertEqual(cs.project_dirs("/a/b", root), [short])
            self.assertEqual(cs.project_dirs("/a/c", root), [])
            self.assertEqual(cs.project_dirs(None, root), [])

    def test_context_limit(self):
        for model, want in (("claude-sonnet-4-5-20250929", 200_000), ("claude-opus-4-1-20250805", 200_000),
                            ("claude-sonnet-4-20250514", 200_000), ("claude-3-5-sonnet-20241022", 200_000),
                            ("claude-sonnet-4-5[1m]", 1_000_000), ("claude-opus-4-7", 1_000_000),
                            ("claude-opus-4-7-20260101", 1_000_000), ("claude-opus-5", 1_000_000),
                            ("claude-sonnet-5-1", 1_000_000), ("claude-haiku-5", 1_000_000),
                            ("claude-fable-1", 1_000_000), ("weird", 200_000), (None, None)):
            self.assertEqual(cs.context_limit(model), want, model)

    def test_is_interrupt(self):
        self.assertTrue(cs.is_interrupt({"content": "[Request interrupted by user]"}))
        self.assertTrue(cs.is_interrupt({"content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}))
        self.assertTrue(cs.is_interrupt({"content": [{"type": "tool_result", "content": [
            {"type": "text", "text": "[Request interrupted by user for tool use]"}]}]}))
        self.assertFalse(cs.is_interrupt({"content": "please interrupt nothing"}))
        self.assertFalse(cs.is_interrupt(None))


class SummaryTest(unittest.TestCase):
    def summary(self, recs):
        return cs.summarize_records(recs, recs)

    def test_fixture(self):
        cs._cache.clear()
        s = cs.read_transcript(os.path.join(FIXTURES, "transcript.jsonl"))
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["ai_title"], "Token refresh hook")
        self.assertEqual(s["first_prompt"], "Refactor the token refresh")      # one line, meta skipped
        self.assertEqual(s["last_message"], "Moved the refresh logic into useTokenRefresh.")
        self.assertEqual(s["context_tokens"], 42_010)                         # sidechain usage ignored
        self.assertEqual(s["model"], "claude-sonnet-4-5-20250929")
        self.assertEqual(s["session_id"], SID)
        self.assertFalse(s["interrupted"])

    def test_title_priority(self):
        base = [user("first prompt here", NOW - 100), assistant(NOW - 90, "hi")]
        self.assertEqual(self.summary(base)["first_prompt"], "first prompt here")
        recs = base + [{"type": "summary", "summary": "A summary"}]
        self.assertEqual(self.summary(recs)["summary"], "A summary")
        recs += [{"type": "ai-title", "aiTitle": "AI title"}, {"type": "custom-title", "customTitle": "Mine"},
                 {"type": "ai-title", "aiTitle": "Later AI title"}]
        s = self.summary(recs)
        self.assertEqual((s["custom_title"], s["ai_title"]), ("Mine", "Later AI title"))
        s = self.summary(recs + [{"type": "custom-title", "customTitle": "Renamed"}])
        self.assertEqual(s["custom_title"], "Renamed")                         # latest wins

    def test_states(self):
        cases = [
            ([user("go", NOW)], "busy"),
            ([user("go", NOW), assistant(NOW, "x", tool=True, stop="tool_use")], "busy"),
            ([user("go", NOW), assistant(NOW, tool=True, stop=None)], "busy"),
            ([user("go", NOW), assistant(NOW, "x", tool=True), tool_result(NOW)], "busy"),
            ([user("go", NOW), assistant(NOW, "done")], "idle"),
            ([user("go", NOW), assistant(NOW, "partial", stop=None)], "stream"),
            ([user("go", NOW), assistant(NOW, tool=True), user("[Request interrupted by user for tool use]", NOW)], "idle"),
            ([user("go", NOW), assistant(NOW, "done"), assistant(NOW, "side", tool=True, sidechain=True)], "idle"),
            ([], None),
        ]
        for recs, want in cases:
            with self.subTest(recs=[r["type"] for r in recs]):
                self.assertEqual(self.summary(recs)["state"], want)
        s = self.summary([user("go", NOW), user("[Request interrupted by user]", NOW)])
        self.assertTrue(s["interrupted"])

    def test_context_tokens_last_main_chain_assistant(self):
        u1 = {"input_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10}
        u2 = {"input_tokens": 2, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 20}
        recs = [assistant(NOW, "a", usage=u1), assistant(NOW, "b", usage=u2, model="claude-opus-4-7"),
                assistant(NOW, "c", usage={"input_tokens": 5}, sidechain=True),
                assistant(NOW, "no usage synthetic", usage={"input_tokens": 0}, model="<synthetic>")]
        s = self.summary(recs)
        self.assertEqual((s["context_tokens"], s["model"]), (222, "claude-opus-4-7"))
        self.assertEqual(s["last_message"], "no usage synthetic")

    def test_transcript_status(self):
        def st(state, age, **kw):
            return cs.transcript_status({"state": state, "mtime": NOW - age, **kw}, NOW)
        self.assertEqual(st("idle", 60), ("idle", 60))
        self.assertEqual(st("idle", 31 * 60), ("done", 31 * 60))
        self.assertEqual(st("busy", 60), ("busy", 60))
        self.assertEqual(st("busy", 60, last_prompt_ts=NOW - 200), ("busy", 200))
        self.assertEqual(st("busy", cs.BUSY_WINDOW_S + 1), ("unknown", None))      # stale: unknown
        self.assertEqual(st("stream", 5), ("busy", 5))
        self.assertEqual(st("stream", cs.STREAM_QUIET_S + 1), ("idle", cs.STREAM_QUIET_S + 1))
        self.assertEqual(st(None, 5), ("unknown", None))

    def test_hook_status(self):
        self.assertEqual(cs.hook_status({"status": "busy", "since": NOW - 5, "ts": NOW}, NOW), ("busy", 5))
        self.assertEqual(cs.hook_status({"status": "idle", "since": NOW - 3600}, NOW), ("done", 3600))
        self.assertEqual(cs.hook_status({"status": "needs_input", "ts": NOW - 2}, NOW), ("needs_input", 2))
        self.assertEqual(cs.hook_status({"status": "ended", "ts": NOW}, NOW), ("unknown", None))


class TranscriptReadTest(_Env):
    def test_big_file_reads_head_and_tail_only(self):
        filler = assistant(NOW - 500, "x" * 1000, tool=True, stop="tool_use")
        recs = [user("the very first prompt", NOW - 600)] + [filler] * 600 + [assistant(NOW - 1, "final words")]
        path = self.transcript(recs)
        self.assertGreater(os.path.getsize(path), cs.HEAD_BYTES + cs.TAIL_BYTES)
        real_open = open
        reads = []

        class Spy:
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.fh.close()

            def read(self, n=-1):
                self.assertn = n
                reads.append(n)
                return self.fh.read(n)

            def seek(self, *a):
                return self.fh.seek(*a)
        with mock.patch("builtins.open", lambda p, mode="r", *a, **kw: Spy(real_open(p, mode, *a, **kw))):
            s = cs.read_transcript(path)
        self.assertTrue(all(0 <= n <= cs.TAIL_BYTES + 1 for n in reads), reads)
        self.assertEqual(s["first_prompt"], "the very first prompt")
        self.assertEqual((s["state"], s["last_message"]), ("idle", "final words"))

    def test_cache_and_torn_line(self):
        path = self.transcript([user("go", NOW), assistant(NOW, "done")], raw_tail='{"type": "assis')
        a = cs.read_transcript(path)
        self.assertEqual(a["state"], "idle")
        self.assertIs(cs.read_transcript(path), a)                         # cached by (mtime, size)
        with open(path, "a") as fh:
            fh.write('tant"}\n' + json.dumps(user("again", NOW)) + "\n")
        self.assertEqual(cs.read_transcript(path)["state"], "busy")
        self.assertIsNone(cs.read_transcript(os.path.join(self.tmp, "missing.jsonl")))

    def test_tail_starting_on_line_boundary_keeps_first_line(self):
        # head ends exactly at a newline: the first tail line must not be dropped
        line = json.dumps(assistant(NOW, "pad", tool=True)) + "\n"
        n = cs.HEAD_BYTES // len(line) + 1
        path = self.transcript([assistant(NOW, "pad", tool=True)] * n + [assistant(NOW, "end")])
        with mock.patch.object(cs, "TAIL_BYTES", len(line.replace("pad", "end").replace(', {"type": "tool_use", "id": "x", "name": "Bash", "input": {}}', "").replace('"tool_use"}', '"end_turn"}'))):
            cs._cache.clear()
            self.assertEqual(cs.read_transcript(path)["last_message"], "end")


# --------------------------------------------------------------------------- resolution

class ResolveTest(unittest.TestCase):
    def test_priority(self):
        start = NOW - 100
        argv = ("claude", "--session-id", "A")
        hooks = {"A": {"session_id": "A", "pid": 7, "ts": NOW - 50, "status": "busy"},
                 "B": {"session_id": "B", "pid": 8, "ts": NOW - 5, "status": "idle"}}
        reg = {"sessionId": "R"}
        self.assertEqual(cs.resolve_session(7, argv, {}, start, hooks, reg), ("A", "argv"))       # a)
        self.assertEqual(cs.resolve_session(8, ("claude",), {}, start, hooks, reg), ("B", "hook"))  # b)
        self.assertEqual(cs.resolve_session(9, ("claude",), {}, start, hooks, reg), ("R", "registry"))  # c)
        self.assertEqual(cs.resolve_session(9, ("claude",), {}, start, hooks, None), (None, None))

    def test_hook_of_same_process_overrides_argv_after_clear(self):
        start = NOW - 100
        argv = ("claude", "--session-id", "A")
        hooks = {"A": {"session_id": "A", "pid": 7, "ts": NOW - 50, "status": "ended", "event": "SessionEnd"},
                 "C": {"session_id": "C", "pid": 7, "ts": NOW - 40, "status": "idle"}}
        self.assertEqual(cs.resolve_session(7, argv, {}, start, hooks, None), ("C", "hook"))
        hooks["A"] = {**hooks["A"], "status": "busy", "event": "PreToolUse", "ts": NOW - 30}
        self.assertEqual(cs.resolve_session(7, argv, {}, start, hooks, None), ("A", "argv"))

    def test_hook_matched_by_zellij_pane_and_start_time(self):
        env = {"ZELLIJ_SESSION_NAME": "s", "ZELLIJ_PANE_ID": "4"}
        hooks = {"P": {"session_id": "P", "pid": None, "zellij_session": "s", "zellij_pane": 4, "ts": NOW - 5},
                 "O": {"session_id": "O", "pid": 7, "ts": NOW - 1000}}      # older than the process
        self.assertEqual(cs.resolve_session(7, ("claude",), env, NOW - 100, hooks, None), ("P", "hook"))
        self.assertEqual(cs.resolve_session(7, ("claude",), {}, NOW - 100, hooks, None), (None, None))


# --------------------------------------------------------------------------- fetch

class FetchTest(_Env):
    def test_argv_session_with_hooks(self):
        u = {"input_tokens": 5, "cache_read_input_tokens": 50_000, "cache_creation_input_tokens": 0}
        self.transcript([user("fix the flaky test\nmore", NOW - 100), {"type": "ai-title", "aiTitle": "Flaky test"},
                         assistant(NOW - 50, "Reading", tool=True, stop="tool_use", usage=u)])
        self.hook(status="busy", since=NOW - 30, pid=100, last_message="Hook says hi")
        argv = ("claude", "--settings", "/s.json", "--session-id", SID)
        env = {"ZELLIJ_SESSION_NAME": "web", "ZELLIJ_PANE_ID": "3"}
        agents, warns = self.fetch({100: (argv, env)})
        a = agents[100]
        self.assertEqual(warns, [])
        self.assertEqual((a.provider, a.meta), ("claude", {}))
        self.assertEqual((a.session_name, a.pane_id, a.cwd), ("web", 3, self.work))
        self.assertEqual((a.status, a.status_since_s, a.status_source), ("busy", 30, "hooks"))
        self.assertEqual((a.session_id, a.title, a.last_message), (SID, "Flaky test", "Hook says hi"))
        self.assertEqual((a.context_tokens, a.context_limit), (50_005, 200_000))
        self.assertEqual(a.start_time, NOW - 3600)
        self.assertEqual((a.status_quality, a.status_note), ("exact", None))

    def test_hook_idle_long_is_done_and_needs_input(self):
        self.transcript([user("go", NOW - 4000), assistant(NOW - 3900, "done")])
        self.hook(status="idle", event="Stop", since=NOW - 3000, ts=NOW - 3000)
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual((a.status, a.status_source), ("done", "hooks"))
        self.hook(status="needs_input", event="Notification", since=NOW - 7, ts=NOW - 7)
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual((a.status, a.status_since_s), ("needs_input", 7))

    def test_hook_precedence_over_registry_and_transcript(self):
        self.transcript([user("go", NOW - 20), assistant(NOW - 10, "done")], mtime=NOW - 10)
        self.registry(100, sessionId=SID, status="busy", cwd=self.work)
        self.hook(status="needs_input", event="PermissionRequest", ts=NOW - 5, since=NOW - 5)
        a = self.fetch({100: (("claude",), {})})[0][100]
        self.assertEqual((a.status, a.status_source, a.session_id), ("needs_input", "hooks", SID))

    def test_ended_or_old_hook_record_ignored(self):
        self.transcript([user("go", NOW - 20), assistant(NOW - 10, "all done")], mtime=NOW - 10)
        self.hook(status="ended", event="SessionEnd", ts=NOW - 5, last_message="bye")
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual((a.status, a.status_source), ("idle", "transcript"))
        self.assertEqual(a.last_message, "bye")
        self.hook(status="busy", ts=NOW - 7200, since=NOW - 7200)        # before the process started
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual((a.status, a.status_source, a.last_message), ("idle", "transcript", "all done"))

    def test_interrupt_overrides_busy_hook(self):
        path = self.transcript([user("go", NOW - 60), assistant(NOW - 50, tool=True, stop="tool_use"),
                                user("[Request interrupted by user for tool use]", NOW - 20)], mtime=NOW - 20)
        self.hook(status="busy", event="PreToolUse", ts=NOW - 50, since=NOW - 60)
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual((a.status, a.status_since_s, a.status_source), ("idle", 20, "hooks"))
        # a hook written after the interrupt (new prompt) wins again
        self.hook(status="busy", event="UserPromptSubmit", ts=NOW - 5, since=NOW - 5)
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual(a.status, "busy")
        # no interrupt marker: the busy hook stands even if the transcript is newer
        os.utime(path, (NOW - 1, NOW - 1))
        self.transcript([user("go", NOW - 60), assistant(NOW - 50, tool=True, stop="tool_use")], mtime=NOW - 1)
        self.hook(status="busy", event="PreToolUse", ts=NOW - 50, since=NOW - 60)
        self.assertEqual(self.fetch({100: (("claude", "--session-id", SID), {})})[0][100].status, "busy")

    def test_interrupt_overrides_needs_input_after_idle_prompt(self):
        # permission prompt denied with Esc; ~60 s later Notification(idle_prompt) rewrites the
        # record (new ts) but keeps needs_input and its since
        self.transcript([user("go", NOW - 200), assistant(NOW - 190, tool=True, stop="tool_use"),
                         tool_result(NOW - 120, "[Request interrupted by user for tool use]"),
                         user("[Request interrupted by user for tool use]", NOW - 120)], mtime=NOW - 120)
        self.hook(status="needs_input", event="Notification", since=NOW - 180, ts=NOW - 60)
        a = self.fetch({100: (("claude", "--session-id", SID), {})})[0][100]
        self.assertEqual((a.status, a.status_since_s, a.status_source), ("idle", 120, "hooks"))
        # still waiting (no interrupt marker): needs_input stands
        self.transcript([user("go", NOW - 200), assistant(NOW - 190, tool=True, stop="tool_use")], mtime=NOW - 120)
        self.assertEqual(self.fetch({100: (("claude", "--session-id", SID), {})})[0][100].status, "needs_input")

    def test_registry(self):
        self.transcript([user("go", NOW - 20), assistant(NOW - 10, "done")], mtime=NOW - 10)
        self.registry(100, pid=100, sessionId=SID, status="busy", cwd=self.work, name="Named session",
                      startedAt=(NOW - 3600) * 1000)
        a = self.fetch({100: (("claude",), {})})[0][100]
        self.assertEqual((a.status, a.status_source, a.session_id, a.title), ("busy", "registry", SID, "Named session"))
        self.assertEqual((a.status_quality, a.status_note), ("approximate", "Status is approximate"))
        # pid mismatch inside the file: ignored -> transcript guess
        self.registry(100, pid=999, sessionId="other", status="busy")
        a = self.fetch({100: (("claude",), {})})[0][100]
        self.assertEqual((a.status, a.status_source, a.session_id), ("idle", "transcript", SID))
        self.assertEqual((a.status_quality, a.status_note), ("approximate", "Status is approximate"))
        # left over from an earlier process with the same pid (started long before this one)
        self.registry(100, pid=100, sessionId="stale", status="busy", startedAt=(NOW - 90_000) * 1000)
        a = self.fetch({100: (("claude",), {})})[0][100]
        self.assertEqual((a.status_source, a.session_id), ("transcript", SID))
        # garbage
        with open(os.path.join(self.cdir, "sessions", "100.json"), "w") as fh:
            fh.write("[1, 2")
        self.assertEqual(self.fetch({100: (("claude",), {})})[0][100].status_source, "transcript")

    def test_transcript_guess_newest_per_process_unclaimed(self):
        self.transcript([user("older", NOW - 600), assistant(NOW - 590, "a")], sid="s-old", mtime=NOW - 590)
        self.transcript([user("newer", NOW - 30), assistant(NOW - 20, "b", tool=True)], sid="s-new", mtime=NOW - 20)
        self.transcript([user("ancient", NOW - 9000)], sid="s-ancient", mtime=NOW - 9000)  # before both processes
        agents, _ = self.fetch({100: (("claude",), {}), 101: (("claude",), {})},
                               starts={100: NOW - 1000, 101: NOW - 900})
        self.assertEqual(agents[101].session_id, "s-new")           # newest process, newest transcript
        self.assertEqual(agents[100].session_id, "s-old")
        self.assertEqual((agents[101].status, agents[101].title), ("busy", "newer"))
        self.assertEqual((agents[100].status, agents[100].status_source), ("idle", "transcript"))

    def test_no_session_at_all(self):
        a = self.fetch({100: (("claude",), {})})[0][100]
        self.assertEqual((a.status, a.status_source, a.session_id, a.title), ("unknown", None, None, None))
        self.assertEqual((a.status_quality, a.status_note), ("unavailable", None))

    def test_per_process_claude_config_dir(self):
        other = os.path.join(self.tmp, "other-claude")
        self.transcript([user("in other root", NOW - 30), assistant(NOW - 20, "ok")], root=other, mtime=NOW - 20)
        self.registry(100, root=other, sessionId=SID, status="idle", name="From other root")
        a = self.fetch({100: (("claude",), {"CLAUDE_CONFIG_DIR": other})})[0][100]
        self.assertEqual((a.session_id, a.title, a.status_source), (SID, "From other root", "registry"))
        b = self.fetch({100: (("claude",), {})})[0][100]                # default root: nothing there
        self.assertEqual((b.session_id, b.status_source), (None, None))

    def test_known_pids_path(self):
        procs = {100: (("claude",), {}), 101: (("opencode", "--port", "1"), {}), 102: (None, None)}
        agents, warns = self.fetch(procs, pids=[100, 101, 102, 100])
        self.assertEqual(list(agents), [100])
        self.assertEqual(warns, [])
        agents, warns = self.fetch(procs, pids=[])
        self.assertEqual((agents, warns), ({}, []))

    def test_ps_failure_and_never_raises(self):
        with mock.patch.object(procmod, "list_procs", return_value=[]):
            self.assertEqual(cs.fetch(timeout=0.1), ((), [cs.SourceWarning("claude", "ps failed")]))
        with mock.patch.object(procmod, "list_procs", return_value=[(5, 1, "claude")]), \
                mock.patch.object(procmod, "proc_args_env", return_value=(("claude",), {})), \
                mock.patch.object(procmod, "proc_cwd", side_effect=OSError("lsof gone")):
            agents, warns = cs.fetch(timeout=0.1)
        self.assertEqual(agents, ())
        self.assertIn("lsof gone", warns[0].message)
        # one agent whose resolution crashes still shows up (no status), with a warning
        with mock.patch.object(cs, "_agent", side_effect=ValueError("bad")):
            agents, warns = self.fetch({100: (("claude",), {"ZELLIJ_PANE_ID": "2"})})
        self.assertEqual((agents[100].provider, agents[100].pane_id, agents[100].status), ("claude", 2, "unknown"))
        self.assertIn("bad", warns[0].message)

    def test_discovery_skips_non_sessions(self):
        procs = {100: (("claude", "mcp", "serve"), {}), 101: (("claude", "-p", "hi"), {}),
                 102: (("claude", "update the README"), {})}
        agents, _ = self.fetch(procs)
        self.assertEqual(list(agents), [102])

    def test_fast(self):
        u = {"input_tokens": 1, "cache_read_input_tokens": 1000}
        recs = [user("p", NOW - 100)] + [assistant(NOW - 50, "y" * 2000, tool=True, usage=u)] * 400
        self.transcript(recs, mtime=NOW - 1)
        self.hook(status="busy", ts=NOW - 5)
        procs = {100 + i: (("claude", "--session-id", SID), {}) for i in range(4)}
        self.fetch(procs)                           # warm
        cs._cache.clear()
        t0 = time.perf_counter()
        self.fetch(procs)
        self.assertLess(time.perf_counter() - t0, 0.25)


if __name__ == "__main__":
    unittest.main()
