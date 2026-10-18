import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from agentree.actions import exec as ex


class RunTest(unittest.TestCase):
    def test_ok(self):
        r = ex.run(["sh", "-c", "echo out; echo err >&2"])
        self.assertTrue(r.ok)
        self.assertEqual(r.code, 0)
        self.assertEqual(r.stdout, "out\n")
        self.assertEqual(r.stderr, "err\n")
        self.assertFalse(r.timed_out)
        self.assertEqual(r.argv, ("sh", "-c", "echo out; echo err >&2"))

    def test_nonzero_does_not_raise(self):
        r = ex.run(["sh", "-c", "exit 3"])
        self.assertEqual(r.code, 3)
        self.assertFalse(r.ok)

    def test_timeout_kills_process_group(self):
        t0 = time.monotonic()
        # the background sleep keeps stdout open: without killpg communicate() would hang 5 s
        r = ex.run(["sh", "-c", "sleep 5 & sleep 5"], timeout=0.3)
        elapsed = time.monotonic() - t0
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.code)
        self.assertFalse(r.ok)
        self.assertLess(elapsed, 2.0)

    def test_timeout_grandchild_dead(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            r = ex.run(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"], timeout=0.3)
            self.assertTrue(r.timed_out)
            with open(pidfile) as fh:
                pid = int(fh.read().strip())
            alive = True
            for _ in range(20):  # reparented zombie is reaped by launchd shortly after SIGKILL
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(alive)

    def test_missing_binary(self):
        r = ex.run(["definitely-not-a-binary-agentree-xyz", "--x"])
        self.assertEqual(r.code, 127)
        self.assertFalse(r.ok)
        self.assertIn("not found", r.stderr)

    def test_env_merged_and_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            r = ex.run(["sh", "-c", 'echo "$AGENTREE_TEST_VAR:$HOME"; pwd'], env={"AGENTREE_TEST_VAR": "v"}, cwd=d)
            line1, line2 = r.stdout.splitlines()
            self.assertEqual(line1, f"v:{os.environ['HOME']}")
            self.assertEqual(os.path.realpath(line2), os.path.realpath(d))

    def test_input(self):
        r = ex.run(["cat"], input="hello")
        self.assertEqual(r.stdout, "hello")

    def test_dry_run_logs_and_does_not_execute(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": os.path.join(d, "st")}):
            marker = os.path.join(d, "marker")
            r = ex.run(["touch", marker], dry_run=True)
            self.assertTrue(r.dry_run)
            self.assertEqual(r.code, 0)
            self.assertTrue(r.ok)
            self.assertFalse(os.path.exists(marker))
            with open(os.path.join(d, "st", "log")) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn(" DRY ", lines[0])
            self.assertTrue(lines[0].endswith(f"touch {marker}"))

    def test_log_flag(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AGENTREE_STATE_DIR": d}):
            ex.run(["sh", "-c", "exit 2"], log=True)
            ex.run(["true"])  # not logged
            with open(os.path.join(d, "log")) as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 1)
            parts = lines[0].split(" ")
            self.assertEqual(parts[1], "2")
            self.assertTrue(parts[2].endswith("s"))
            self.assertIn("sh -c 'exit 2'", lines[0])

    def test_which(self):
        self.assertIsNotNone(ex.which("sh"))
        self.assertIsNone(ex.which("definitely-not-a-binary-agentree-xyz"))
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIsNone(ex.which("definitely-not-a-binary-agentree-xyz"))


class RegistryTest(unittest.TestCase):
    def tearDown(self):
        ex._reopen()

    def test_shutdown_kills_running_groups_and_blocks_new_runs(self):
        import threading

        res = {}

        def worker():
            # grandchild shares the group: killpg must take it down too
            res["r"] = ex.run(["sh", "-c", "sleep 5 & sleep 5"], timeout=10)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        deadline = time.monotonic() + 2
        while ex.live_processes() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(ex.live_processes(), 1)
        t0 = time.monotonic()
        self.assertEqual(ex.shutdown(), 1)
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertFalse(res["r"].ok)
        self.assertFalse(res["r"].timed_out)
        self.assertEqual(ex.live_processes(), 0)
        r = ex.run(["sh", "-c", "echo hi"])
        self.assertEqual((r.code, r.stdout), (ex.SHUTDOWN_CODE, ""))
        self.assertEqual(ex.shutdown(), 0)              # idempotent

    def test_registry_empty_after_normal_runs(self):
        ex.run(["sh", "-c", "exit 0"])
        ex.run(["sh", "-c", "sleep 5"], timeout=0.1)
        self.assertEqual(ex.live_processes(), 0)


def _wait_dead(pid: int, timeout: float = 1.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _read_pid(path: str, timeout: float = 1.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with open(path) as fh:
                txt = fh.read().strip()
            if txt:
                return int(txt)
        except FileNotFoundError:
            pass
        time.sleep(0.01)
    raise AssertionError("no pid file")


class LingeringDescendantTest(unittest.TestCase):
    """A leader that exits while a descendant keeps the output pipes open (`cmd &`)."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.pidfile = os.path.join(self.d.name, "pid")
        self.addCleanup(self.d.cleanup)
        self.addCleanup(ex._reopen)

    def script(self, tail: str = "exit 0") -> list[str]:
        return ["sh", "-c", f"echo hi; sleep 8 & echo $! > {self.pidfile}; {tail}"]

    def check(self, fn, timeout, *, timed_out, code, max_s, **kw):
        t0 = time.monotonic()
        r = fn(self.script(), timeout=timeout, **kw)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, max_s)
        self.assertEqual((r.timed_out, r.code), (timed_out, code))
        self.assertTrue(r.stdout.startswith("hi"))
        self.assertTrue(_wait_dead(_read_pid(self.pidfile)), "grandchild survived")
        self.assertEqual(ex.live_processes(), 0)
        return elapsed

    def test_run_stream_timeout_reproduction(self):
        # was > 3 s (reader thread blocked on the buffered pipe, close() waiting for its lock)
        self.check(ex.run_stream, 0.2, timed_out=True, code=None, max_s=0.6, log=False)

    def test_run_stream_normal_exit_lingers_then_kills_group(self):
        e = self.check(ex.run_stream, 5.0, timed_out=False, code=0, max_s=1.0, log=False)
        self.assertGreaterEqual(e, ex.LINGER_S)

    def test_run_timeout(self):
        self.check(ex.run, 0.2, timed_out=True, code=None, max_s=0.6)

    def test_run_normal_exit_lingers_then_kills_group(self):
        e = self.check(ex.run, 5.0, timed_out=False, code=0, max_s=1.0)
        self.assertGreaterEqual(e, ex.LINGER_S)

    def test_exit_code_of_leader_kept(self):
        r = ex.run(["sh", "-c", "sleep 8 & exit 4"], timeout=5)
        self.assertEqual((r.code, r.timed_out), (4, False))

    def test_output_written_during_linger_is_kept(self):
        r = ex.run_stream(["sh", "-c", "(sleep 0.1; echo late) & echo early; exit 0"], timeout=5, log=False)
        self.assertEqual(r.stdout.splitlines(), ["early", "late"])
        self.assertEqual(r.code, 0)

    def test_escaped_descendant_does_not_block(self):
        # the grandchild leaves the process group (setsid), so killpg cannot reach it; the pipe
        # it holds must still be abandoned promptly
        code = f"import os,time; os.setsid(); open({self.pidfile!r},'w').write(str(os.getpid())); time.sleep(8)"
        argv = ["sh", "-c", f"{sys.executable} -c \"{code}\" & exit 0"]
        for fn in (ex.run, ex.run_stream):
            with self.subTest(fn=fn.__name__):
                if os.path.exists(self.pidfile):
                    os.unlink(self.pidfile)
                t0 = time.monotonic()
                r = fn(argv, timeout=5, **({"log": False} if fn is ex.run_stream else {}))
                elapsed = time.monotonic() - t0
                pid = _read_pid(self.pidfile)
                os.kill(pid, 9)
                self.assertLess(elapsed, ex.LINGER_S + ex.KILL_DRAIN_S + 0.4)
                self.assertEqual(r.code, 0)
                self.assertEqual(ex.live_processes(), 0)

    def test_registered_until_cleanup_and_shutdown_kills_lingering_group(self):
        import threading
        res = {}
        t = threading.Thread(target=lambda: res.setdefault("r", ex.run_stream(
            ["sh", "-c", f"sleep 8 & echo $! > {self.pidfile}; exit 0"], timeout=10, log=False)), daemon=True)
        with mock.patch.object(ex, "LINGER_S", 5.0):
            t.start()
            pid = _read_pid(self.pidfile)
            time.sleep(0.2)                              # leader has exited; group still lingering
            self.assertEqual(ex.live_processes(), 1)
            self.assertEqual(ex.shutdown(), 1)
            t.join(1.0)
        self.assertFalse(t.is_alive())
        self.assertTrue(_wait_dead(pid))
        self.assertEqual(ex.live_processes(), 0)


class RunStreamTest(unittest.TestCase):
    def test_lines_callback_and_tail(self):
        seen = []
        r = ex.run_stream(["sh", "-c", "echo a; echo; echo b >&2; printf 'c\\rd\\r\\ne'"], on_line=seen.append,
                          log=False, keep=3)
        self.assertEqual(r.code, 0)
        self.assertEqual(seen, ["a", "b", "c", "d", "e"])
        self.assertEqual(r.stdout, "c\nd\ne")
        self.assertEqual(r.stderr, "")

    def test_cancel_mid_stream(self):
        import threading
        cancel = threading.Event()
        seen = []

        def on_line(line):
            seen.append(line)
            if len(seen) == 3:
                cancel.set()
        with tempfile.TemporaryDirectory() as d:
            pidfile = os.path.join(d, "pid")
            t0 = time.monotonic()
            r = ex.run_stream(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; "
                                           "i=0; while true; do i=$((i+1)); echo line $i; sleep 0.02; done"],
                              on_line=on_line, cancel=cancel, timeout=30, log=False)
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertTrue(r.cancelled)
            self.assertFalse(r.timed_out)
            self.assertIsNone(r.code)
            self.assertFalse(r.ok)
            self.assertEqual(seen[:3], ["line 1", "line 2", "line 3"])
            self.assertTrue(_wait_dead(_read_pid(pidfile)))
        self.assertEqual(ex.live_processes(), 0)

    def test_large_output(self):
        n = 50000
        seen = []
        t0 = time.monotonic()
        r = ex.run_stream([sys.executable, "-c", f"import sys\nfor i in range({n}): print('x' * 60, i)\n"
                                                 "sys.stdout.flush()"], on_line=seen.append, log=False, keep=5)
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(r.code, 0)
        self.assertEqual(len(seen), n)
        self.assertEqual(r.stdout.splitlines()[-1], "x" * 60 + f" {n - 1}")
        self.assertEqual(len(r.stdout.splitlines()), 5)

    def test_run_large_stdout_and_stderr_and_input(self):
        code = "import sys; d=sys.stdin.read(); sys.stdout.write(d*2); sys.stderr.write('e'*1000000)"
        r = ex.run([sys.executable, "-c", code], input="ab" * 500000, timeout=10)
        self.assertEqual(r.code, 0)
        self.assertEqual(len(r.stdout), 2000000)
        self.assertEqual(len(r.stderr), 1000000)

    def test_run_input_ignored_by_child(self):
        r = ex.run(["sh", "-c", "exec 0<&-; echo done"], input="x" * 1000000, timeout=3)
        self.assertEqual((r.code, r.stdout), (0, "done\n"))

    def test_run_universal_newlines_and_utf8(self):
        r = ex.run(["printf", "a\\r\\nb\\rc\\n\\377\\303\\251"])
        self.assertEqual(r.stdout, "a\nb\nc\n\ufffdé")

    def test_missing_binary(self):
        r = ex.run_stream(["definitely-not-a-binary-agentree-xyz"], log=False)
        self.assertEqual(r.code, 127)


FLOOD = "import os\nb = b'x' * 65536\nwhile True: os.write(1, b)\n"   # full chunks, no line ends


class FastProducerTest(unittest.TestCase):
    """A child writing as fast as it can must not starve the deadline / cancel checks."""

    def flood(self, fn, **kw):
        t0 = time.monotonic()
        r = fn(**kw)
        return r, time.monotonic() - t0

    def test_run_timeout_with_flood(self):
        r, e = self.flood(lambda: ex.run([sys.executable, "-c", FLOOD], timeout=0.3))
        self.assertLess(e, 0.6)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.code)
        self.assertLessEqual(len(r.stdout), ex.MAX_OUTPUT + 200)
        self.assertEqual(ex.live_processes(), 0)

    def test_run_stream_timeout_with_flood(self):
        for argv in ([sys.executable, "-c", FLOOD], ["yes"]):
            with self.subTest(argv=argv[0]):
                n = []
                r, e = self.flood(lambda: ex.run_stream(argv, timeout=0.3, log=False, on_line=n.append))
                self.assertLess(e, 0.6)
                self.assertTrue(r.timed_out)
                self.assertIsNone(r.code)
                self.assertEqual(ex.live_processes(), 0)

    def test_run_stream_cancel_with_flood(self):
        import threading
        for argv in ([sys.executable, "-c", FLOOD], ["yes"]):
            with self.subTest(argv=argv[0]):
                cancel = threading.Event()
                threading.Timer(0.2, cancel.set).start()
                r, e = self.flood(lambda: ex.run_stream(argv, timeout=30, cancel=cancel, log=False))
                self.assertLess(e, 0.5)
                self.assertTrue(r.cancelled)
                self.assertFalse(r.timed_out)
                self.assertIsNone(r.code)
                self.assertEqual(ex.live_processes(), 0)

    def test_50mb_without_line_ends(self):
        code = "import sys; sys.stdout.buffer.write(b'x' * 50_000_000 + b'END')"
        r, e = self.flood(lambda: ex.run_stream([sys.executable, "-c", code], timeout=10, log=False))
        self.assertLess(e, 2.0)
        self.assertEqual(r.code, 0)
        self.assertEqual(len(r.stdout), 1 + ex.MAX_LINE)                # "…" + the last MAX_LINE bytes
        self.assertTrue(r.stdout.startswith("…x") and r.stdout.endswith("xEND"))
        r, e = self.flood(lambda: ex.run([sys.executable, "-c", code], timeout=10))
        self.assertLess(e, 2.0)
        self.assertEqual(r.code, 0)
        self.assertTrue(r.stdout.startswith("x" * ex.MAX_OUTPUT + "\n… [agentree: output truncated, "))
        self.assertIn(f"{50_000_003 - ex.MAX_OUTPUT} more bytes dropped]", r.stdout)
        self.assertLess(len(r.stdout), ex.MAX_OUTPUT + 200)


class LinesTest(unittest.TestCase):
    def collect(self, chunks, **kw):
        seen = []
        ls = ex._Lines(seen.append, 100, **kw)
        for c in chunks:
            ls.feed(c)
        ls.finish()
        return seen, ls

    def test_line_ends_split_anywhere(self):
        data = b"a\r\nb\rc\n\nd\r\r\ne\xc3\xa9"
        want = ["a", "b", "c", "d", "e\u00e9"]
        for size in (1, 2, 3, len(data)):
            with self.subTest(size=size):
                seen, ls = self.collect([data[i:i + size] for i in range(0, len(data), size)])
                self.assertEqual(seen, want)
                self.assertEqual(ls.tail, ["a", "b", "c", "", "d", "", "e\u00e9"])

    def test_long_line_keeps_tail_bounded(self):
        seen = []
        ls = ex._Lines(seen.append, 10, max_line=1000)
        for i in range(500):
            ls.feed(b"%04d" % i * 25)          # 100 bytes per chunk, no line end
            self.assertLessEqual(len(ls.buf), 1000)
        ls.feed(b"!\nnext\n")
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0].startswith("…"))
        self.assertTrue(seen[0].endswith("0499!"))
        self.assertEqual(len(seen[0]), 1 + 1000)
        self.assertEqual(seen[1], "next")        # marker only on the truncated line
        seen, _ = self.collect([b"y" * 3000 + b"\nz\n"], max_line=1000)     # long line inside one chunk
        self.assertEqual(seen, ["…" + "y" * 1000, "z"])

    def test_linear_time(self):
        ls = ex._Lines(None, 5)
        chunk = b"x" * 65536
        t0 = time.monotonic()
        for _ in range(800):                       # 50 MiB, no line end
            ls.feed(chunk)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertLessEqual(len(ls.buf), ex.MAX_LINE)

    def test_capped(self):
        c = ex._Capped(limit=10)
        for part in (b"12345", b"6789", b"abc", b"def"):
            c.feed(part)
        self.assertEqual(bytes(c.data), b"123456789a")
        self.assertEqual(c.dropped, 5)
        self.assertEqual(c.text(), "123456789a\n… [agentree: output truncated, 5 more bytes dropped]\n")
        self.assertEqual(ex._Capped(limit=10).text(), "")


if __name__ == "__main__":
    unittest.main()
