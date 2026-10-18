"""Subprocess wrapper: process-group isolation, hard timeouts, concurrent pipe draining,
dry-run and an append-only action log.

`run()` never raises for non-zero exits, timeouts or missing binaries; inspect `Result`.

Every child started by `run()` is kept in a process registry until it is reaped. `shutdown()`
SIGKILLs the process group of every registered child and makes later `run()` calls fail fast;
the CLI calls it on exit so abandoned background work (a refresh still running when the user
quits) never outlives agentree.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

EXTRA_PATH = (
    "/opt/homebrew/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.nix-profile/bin"),
    "/Applications/kitty.app/Contents/MacOS",
)

_log_lock = threading.Lock()

# --------------------------------------------------------------------------- process registry
_procs_lock = threading.Lock()
_procs: dict[int, subprocess.Popen] = {}   # pid -> live child (pgid == pid: start_new_session)
_closed = False
SHUTDOWN_CODE = 130


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    dry_run: bool = False
    cancelled: bool = False       # run_stream: stopped because `cancel` was set

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def state_dir() -> str:
    """agentree state directory (see agentree.paths)."""
    from agentree.paths import state_dir

    return state_dir()


def log_path() -> str:
    return os.path.join(state_dir(), "log")


def format_command(argv: Sequence[str], env: Mapping[str, str] | None = None, cwd: str | None = None) -> str:
    """Shell-pasteable form of a command: `[cd DIR &&] K=V ... argv` (env overrides first)."""
    pre = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    cmd = shlex.join(argv)
    cmd = f"{pre} {cmd}" if pre else cmd
    return f"cd {shlex.quote(cwd)} && {cmd}" if cwd else cmd


def log_line(text: str, status: str = "0", duration_s: float = 0.0) -> None:
    """Append a free-form line (internal, non-subprocess steps such as file writes)."""
    _write_log(f"{status} {duration_s:.3f}s {text}")


def _write_log(body: str) -> None:
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        ts = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with _log_lock, open(os.path.join(d, "log"), "a", encoding="utf-8") as fh:
            fh.write(f"{ts} {body}\n")
    except OSError:
        pass  # logging must never break an action


def _log(res: Result, env: Mapping[str, str] | None = None, cwd: str | None = None) -> None:
    status = "DRY" if res.dry_run else ("CANCEL" if res.cancelled else
                                        "TIMEOUT" if res.timed_out else str(res.code))
    _write_log(f"{status} {res.duration_s:.3f}s {format_command(res.argv, env, cwd)}")


def which(name: str) -> str | None:
    """shutil.which with a few well-known extra locations appended to PATH."""
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(os.pathsep) if p]
    for extra in EXTRA_PATH:
        if extra not in parts:
            parts.append(extra)
    return shutil.which(name, path=os.pathsep.join(parts))


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session => pgid == pid
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def live_processes() -> int:
    """Number of registered children not yet reaped."""
    with _procs_lock:
        return len(_procs)


def shutdown() -> int:
    """Kill the process group of every registered child; later run() calls fail immediately
    with code SHUTDOWN_CODE. Idempotent. Returns the number of groups signalled."""
    global _closed
    with _procs_lock:
        _closed = True
        procs = list(_procs.values())
    n = 0
    for p in procs:
        # not reaped yet, or reaped but its group may still hold descendants being drained
        # (a pgid is not reused while the group has members)
        if p.returncode is None or getattr(p, "_agentree_group_live", False):
            _kill_group(p)
            n += 1
    return n


def _reopen() -> None:
    """Tests only: undo shutdown()."""
    global _closed
    with _procs_lock:
        _closed = False


def _register(proc: subprocess.Popen) -> bool:
    """Register a child; False if shutdown() already ran (the caller must kill it)."""
    with _procs_lock:
        proc._agentree_group_live = True  # type: ignore[attr-defined]
        _procs[proc.pid] = proc
        return not _closed


def _unregister(proc: subprocess.Popen) -> None:
    with _procs_lock:
        proc._agentree_group_live = False  # type: ignore[attr-defined]
        _procs.pop(proc.pid, None)


# --------------------------------------------------------------------------- pipe pump
#
# run() and run_stream() share one draining loop on the calling thread: non-blocking raw fds +
# poll(2) and os.read, no reader threads and no buffered streams (closing a buffered pipe while
# another thread blocks in it deadlocks on the buffer lock). One overall deadline (and `cancel`)
# covers both the leader's exit and pipe EOF:
#
#   leader exits, pipes reach EOF            -> "exit"   (normal case)
#   leader exits, a descendant keeps a pipe  -> drain at most LINGER_S more, then kill the
#                                               group -> "linger" (exit code of the leader kept)
#   deadline                                 -> "timeout" (group killed)
#   cancel set                               -> "cancel"  (group killed)
#
# After a kill the pipes are drained for at most KILL_DRAIN_S (a descendant that escaped the
# group may hold them forever), then closed; the child stays in the registry until reaped.

LINGER_S = 0.5
KILL_DRAIN_S = 0.2
_SLICE_S = 0.05            # leader-exit / cancel polling granularity while pipes are open
_READ = 65536
_READS_PER_TURN = 4        # <= 256 KiB per fd per pump turn, then back to the deadline/cancel checks
MAX_LINE = 64 * 1024       # run_stream: longest pending (unterminated) line kept; longer -> tail only
MAX_OUTPUT = 16 * 1024 * 1024  # run(): bytes retained per stream (stdout, stderr); the rest is counted


def _popen(args: tuple[str, ...], env: Mapping[str, str] | None, cwd: str | None, *, stdin: bool,
           merge: bool) -> subprocess.Popen:
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge else subprocess.PIPE,
        env=full_env,
        cwd=cwd,
        start_new_session=True,
        bufsize=0,               # raw FileIO objects: no buffer locks
    )


def _pump(proc: subprocess.Popen, outputs: dict[int, Callable[[bytes], None]], *, deadline: float,
          cancel: threading.Event | None = None, stdin: Any = None, stdin_data: bytes = b"",
          linger_s: float = LINGER_S) -> str:
    """Drain `outputs` (fd -> sink) and feed `stdin_data` to the `stdin` file object (closed via
    its own close(), never by fd number: the number could be reused by another thread). Returns
    the outcome (see above); kills the process group unless it is "exit". Output fds stay open."""
    stdin_fd = stdin.fileno() if stdin is not None else None
    poller = select.poll()
    open_fds = dict(outputs)
    for fd in open_fds:
        os.set_blocking(fd, False)
        poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
    pending = memoryview(stdin_data)
    if stdin_fd is not None:
        if pending:
            os.set_blocking(stdin_fd, False)
            poller.register(stdin_fd, select.POLLOUT | select.POLLERR)
        else:
            _close_file(stdin)       # nothing to write: EOF right away
            stdin_fd = None

    def read(fd: int) -> bool:
        """Read at most _READS_PER_TURN chunks (a fast producer must not starve the deadline and
        cancel checks); False at EOF."""
        for _ in range(_READS_PER_TURN):
            try:
                data = os.read(fd, _READ)
            except BlockingIOError:
                return True
            except OSError:
                return False
            if not data:
                return False
            try:
                open_fds[fd](data)
            except Exception:  # noqa: BLE001 - a sink must not break draining
                pass
            if len(data) < _READ:
                return True
        return True

    exited_at: float | None = None
    outcome = "exit"
    while True:
        now = time.monotonic()
        if exited_at is None and proc.poll() is not None:
            exited_at = now
        if exited_at is not None and not open_fds:
            break
        if cancel is not None and cancel.is_set():
            outcome = "cancel"
            break
        if now >= deadline:
            outcome = "timeout"
            break
        if exited_at is not None and now - exited_at >= linger_s:
            outcome = "linger"
            break
        wait = min(deadline - now, _SLICE_S)
        if exited_at is not None:
            wait = min(wait, linger_s - (now - exited_at))
        if not open_fds and stdin_fd is None:
            try:
                proc.wait(timeout=max(0.0, wait))
            except subprocess.TimeoutExpired:
                pass
            continue
        for fd, ev in poller.poll(max(0, int(wait * 1000) + 1)):
            if fd == stdin_fd:
                try:
                    n = os.write(fd, pending[:select.PIPE_BUF * 16])
                    pending = pending[n:]
                except BlockingIOError:
                    continue
                except OSError:          # EPIPE: the child closed stdin
                    pending = pending[:0]
                if not pending:
                    poller.unregister(fd)
                    _close_file(stdin)
                    stdin_fd = None
            elif fd in open_fds and not read(fd):
                poller.unregister(fd)
                del open_fds[fd]
    if outcome != "exit":
        _kill_group(proc)
        end = time.monotonic() + KILL_DRAIN_S
        while open_fds and time.monotonic() < end:
            for fd, _ev in poller.poll(max(1, int((end - time.monotonic()) * 1000))):
                if fd in open_fds and not read(fd):
                    poller.unregister(fd)
                    del open_fds[fd]
    if stdin_fd is not None:
        _close_file(stdin)
    return outcome


def _close_file(f: Any) -> None:
    try:
        f.close()                # raw FileIO: no lock, never blocks; idempotent
    except OSError:
        pass


def _close_pipes(proc: subprocess.Popen) -> None:
    for f in (proc.stdin, proc.stdout, proc.stderr):
        if f is not None:
            _close_file(f)


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=1.0)   # killed or exited: immediate
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass


def _finish(proc: subprocess.Popen, outcome: str) -> None:
    """Close the pipes, reap, and only then leave the registry (an unexpected error in the pump
    kills the group first)."""
    try:
        if outcome == "error":
            _kill_group(proc)
        _close_pipes(proc)
        _reap(proc)
    finally:
        _unregister(proc)


def _text(data: bytes | bytearray) -> str:
    """Decode like text-mode Popen: UTF-8 (replace) with universal newlines."""
    return bytes(data).decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")


def _spawn_failed(args: tuple[str, ...], e: OSError, t0: float) -> Result:
    if isinstance(e, FileNotFoundError):
        return Result(args, 127, "", f"{args[0] if args else ''}: not found ({e.strerror})", False,
                      time.monotonic() - t0)
    return Result(args, 126, "", f"{args[0] if args else ''}: {e}", False, time.monotonic() - t0)


def run(
    argv: Sequence[str],
    *,
    timeout: float = 1.0,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    input: str | None = None,  # noqa: A002 - mirrors subprocess
    dry_run: bool = False,
    log: bool = False,
) -> Result:
    """Run to completion within `timeout` (whole exchange: exit + pipe EOF). On timeout the
    process group is killed (code None, timed_out). A leader that exits while a descendant keeps
    its pipes open gets LINGER_S of extra draining, then the group is killed; its own exit code
    is reported."""
    args = tuple(str(a) for a in argv)
    if dry_run:
        res = Result(args, 0, "", "", False, 0.0, dry_run=True)
        _log(res, env, cwd)
        return res
    t0 = time.monotonic()
    if _closed:
        return Result(args, SHUTDOWN_CODE, "", "agentree is shutting down", False, 0.0)
    try:
        proc = _popen(args, env, cwd, stdin=input is not None, merge=False)
    except OSError as e:
        res = _spawn_failed(args, e, t0)
        if log:
            _log(res, env, cwd)
        return res
    if not _register(proc):
        _kill_group(proc)
    out, err = _Capped(), _Capped()
    outcome = "error"
    try:
        outcome = _pump(proc, {proc.stdout.fileno(): out.feed, proc.stderr.fileno(): err.feed},  # type: ignore[union-attr]
                        deadline=t0 + timeout, stdin=proc.stdin, stdin_data=(input or "").encode("utf-8"))
    finally:
        _finish(proc, outcome)
    timed_out = outcome == "timeout"
    res = Result(args, None if timed_out else proc.returncode, out.text(), err.text(), timed_out,
                 time.monotonic() - t0)
    if log:
        _log(res, env, cwd)
    return res


class _Capped:
    """run() output sink: keeps the first MAX_OUTPUT bytes, counts the rest."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = MAX_OUTPUT if limit is None else limit
        self.data = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> None:
        room = self.limit - len(self.data)
        if room >= len(chunk):
            self.data += chunk
        else:
            if room > 0:
                self.data += chunk[:room]
            self.dropped += len(chunk) - max(room, 0)

    def text(self) -> str:
        t = _text(self.data)
        return t + f"\n… [agentree: output truncated, {self.dropped} more bytes dropped]\n" if self.dropped else t


_EOL = re.compile(rb"\r\n|\r|\n")


class _Lines:
    """Merged output -> lines (universal newlines), last `keep` kept, non-blank ones reported.

    Linear time: only new bytes are scanned for line ends. Bounded memory: a line longer than
    MAX_LINE keeps only its last MAX_LINE bytes (the useful end of a progress line) and is
    emitted with a leading "…"."""

    def __init__(self, on_line: Callable[[str], None] | None, keep: int, max_line: int | None = None) -> None:
        self.on_line, self.keep = on_line, keep
        self.max_line = MAX_LINE if max_line is None else max_line
        self.tail: list[str] = []
        self.buf = b""               # pending partial line (no line end inside, except a final \r)
        self.truncated = False       # the pending line lost its head

    def feed(self, data: bytes) -> None:
        if not data:
            return
        # a pending trailing \r may pair with a leading \n: rescan just that byte
        scan = len(self.buf) - 1 if self.buf.endswith(b"\r") else len(self.buf)
        buf = self.buf + data
        pos = 0
        n = len(buf)
        for m in _EOL.finditer(buf, scan):
            if m.end() == n and buf[m.start()] == 13 and m.end() - m.start() == 1:
                break                # maybe CRLF split across reads: wait for the next byte
            if m.start() - pos > self.max_line:
                self.truncated = True
                self._emit(buf[m.start() - self.max_line:m.start()])
            else:
                self._emit(buf[pos:m.start()])
            pos = m.end()
        rest = buf[pos:]
        if len(rest) > self.max_line:
            rest = rest[-self.max_line:]
            self.truncated = True
        self.buf = rest

    def finish(self) -> None:
        if self.buf:
            b = self.buf[:-1] if self.buf.endswith(b"\r") else self.buf
            self.buf = b""
            self._emit(b)

    def _emit(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace")
        if self.truncated:
            line = "…" + line
            self.truncated = False
        self.tail.append(line)
        if len(self.tail) > self.keep:
            del self.tail[:-self.keep]
        if self.on_line is not None and line.strip():
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001 - progress must not break the command
                pass


def run_stream(
    argv: Sequence[str],
    *,
    timeout: float = 600.0,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    on_line: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    dry_run: bool = False,
    log: bool = True,
    keep: int = 200,
) -> Result:
    """Long-running command (installs): stdout+stderr merged and read line by line, each
    non-blank line passed to `on_line` (on the calling thread); the process group is killed on
    timeout, on `cancel`, on shutdown(), and LINGER_S after the leader exits if a descendant
    still holds the output pipe. Result.stdout holds the last `keep` lines."""
    args = tuple(str(a) for a in argv)
    if dry_run:
        res = Result(args, 0, "", "", False, 0.0, dry_run=True)
        if log:
            _log(res, env, cwd)
        return res
    t0 = time.monotonic()
    if _closed:
        return Result(args, SHUTDOWN_CODE, "", "agentree is shutting down", False, 0.0)
    try:
        proc = _popen(args, env, cwd, stdin=False, merge=True)
    except OSError as e:
        res = _spawn_failed(args, e, t0)
        if log:
            _log(res, env, cwd)
        return res
    if not _register(proc):
        _kill_group(proc)
    lines = _Lines(on_line, keep)
    outcome = "error"
    try:
        outcome = _pump(proc, {proc.stdout.fileno(): lines.feed},  # type: ignore[union-attr]
                        deadline=t0 + timeout, cancel=cancel)
        lines.finish()
    finally:
        _finish(proc, outcome)
    timed_out, cancelled = outcome == "timeout", outcome == "cancel"
    code = None if (timed_out or cancelled) else proc.returncode
    res = Result(args, code, "\n".join(lines.tail), "", timed_out, time.monotonic() - t0, cancelled=cancelled)
    if log:
        _log(res, env, cwd)
    return res
