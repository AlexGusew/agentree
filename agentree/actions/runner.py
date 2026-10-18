"""Command runner shared by the actions: logging, dry-run and test injection in one place.

Two kinds of commands:
  query()  read-only (`list-tabs`, `kitty @ ls`, ...). Always executed, also under --dry-run:
           they have no side effects and every decision (which window, which tab) depends on them.
  act()    side-effecting (`go-to-tab-by-id`, `focus-window`, `launch`, `new-tab`, ...). Under
           --dry-run they are only logged (exec.run(dry_run=True) writes a DRY line).
Both go through exec.run(log=True), so the action log holds the exact command lines.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable, Mapping, Sequence


# --------------------------------------------------------------------------- session identity
# A zellij session is identified by (canonical socket dir, name): the same name can be live in
# two socket dirs. Kept here (import-light) because the CLI's single-instance check uses it on
# the startup path.

def canonical_dir(d: str | None) -> str:
    return os.path.realpath(os.path.expanduser(d)) if d else ""


LINUX = sys.platform.startswith("linux")


def xdg_socket_dir(environ: Mapping[str, str] | None = None) -> str | None:
    """zellij's default socket dir on Linux: $XDG_RUNTIME_DIR/zellij. None on macOS (zellij has no
    runtime dir there) and when the variable is unset (zellij then uses the tmp dir as well)."""
    e = os.environ if environ is None else environ
    d = e.get("XDG_RUNTIME_DIR") if LINUX else None
    return os.path.join(d, "zellij") if d else None


def default_socket_dir(environ: Mapping[str, str] | None = None) -> str:
    """ZELLIJ_SOCKET_DIR, else zellij's default: $XDG_RUNTIME_DIR/zellij on Linux,
    $TMPDIR/zellij-<uid> on macOS."""
    e = os.environ if environ is None else environ
    return (e.get("ZELLIJ_SOCKET_DIR") or xdg_socket_dir(e)
            or os.path.join(e.get("TMPDIR") or "/tmp", f"zellij-{os.getuid()}"))


def identity_key(socket_dir: str | None, name: str) -> str:
    """Stable short key for file names (locks, instance records): sha1(canon dir NUL name)[:16]."""
    try:
        from _sha1 import sha1  # builtin; hashlib costs ~2.5 ms to import
    except ImportError:  # pragma: no cover
        from hashlib import sha1
    return sha1(f"{canonical_dir(socket_dir)}\0{name}".encode()).hexdigest()[:16]


_BTIME: list[float] = []


def _procfs_start_time(pid: int) -> float | None:
    """Linux: boot time (/proc/stat btime) + starttime (field 22 of /proc/<pid>/stat, in clock
    ticks). The comm field may hold spaces and parentheses, so fields are counted after the last ')'."""
    try:
        with open(f"/proc/{int(pid)}/stat", "rb") as fh:
            ticks = int(fh.read().rsplit(b")", 1)[1].split()[19])
        if not _BTIME:
            with open("/proc/stat", "rb") as fh:
                _BTIME.append(next(float(line.split()[1]) for line in fh if line.startswith(b"btime ")))
        return _BTIME[0] + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def proc_start_time(pid: int) -> float | None:
    """Process start time (unix seconds) via sysctl KERN_PROC_PID (kinfo_proc.kp_proc.p_starttime);
    on Linux from /proc."""
    if LINUX:
        return _procfs_start_time(pid)
    import ctypes
    import struct

    libc = ctypes.CDLL(None, use_errno=True)
    mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))          # CTL_KERN, KERN_PROC, KERN_PROC_PID
    buf = ctypes.create_string_buffer(1024)
    size = ctypes.c_size_t(len(buf))
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0 or size.value < 16:
        return None
    sec, usec = struct.unpack_from("qi", buf.raw, 0)     # struct timeval at offset 0
    return sec + usec / 1e6 if sec > 0 else None


_GIT: list[str] = []
_GIT_CANDIDATES = ("/Library/Developer/CommandLineTools/usr/bin/git",
                   "/Applications/Xcode.app/Contents/Developer/usr/bin/git")


def git_bin() -> str:
    """The git to execute. On macOS /usr/bin/git is an xcrun shim costing ~30 ms per call; the
    Command Line Tools binary behind it starts in ~8 ms. Any other git on PATH is used as is."""
    if not _GIT:
        from agentree.actions import exec as ex

        found = ex.which("git") or "git"
        if found == "/usr/bin/git":
            found = next((c for c in _GIT_CANDIDATES if os.access(c, os.X_OK)), found)
        _GIT.append(found)
    return _GIT[0]


def _resolve(argv: list[str]) -> list[str]:
    return [git_bin(), *argv[1:]] if argv and argv[0] == "git" else argv


def _exec_run(argv, **kw):
    from agentree.actions import exec as ex

    return ex.run(_resolve(list(argv)), **kw)


def _exec_stream(argv, **kw):
    from agentree.actions import exec as ex

    return ex.run_stream(_resolve(list(argv)), **kw)


class Runner:
    """query/act/stream/internal. `acted` lists every side-effecting step in order (dry-run count)."""

    def __init__(self, dry_run: bool = False, run_fn: Callable[..., Any] | None = None,
                 stream_fn: Callable[..., Any] | None = None) -> None:
        if run_fn is None:
            run_fn = _exec_run          # real execution: `git` -> git_bin()
        self.dry_run = dry_run
        self._run = run_fn
        self._stream = stream_fn
        self.acted: list[tuple[tuple[str, ...], dict[str, str]]] = []   # side-effecting commands, in order

    def query(self, argv: Sequence[str], env: Mapping[str, str] | None = None, timeout: float = 2.0,
              cwd: str | None = None):
        kw = {"cwd": cwd} if cwd else {}
        return self._run(list(argv), env=dict(env) if env else None, timeout=timeout, log=True, dry_run=False,
                         **kw)

    def act(self, argv: Sequence[str], env: Mapping[str, str] | None = None, timeout: float = 3.0,
            cwd: str | None = None):
        self.acted.append((tuple(argv), dict(env or {})))
        kw = {"cwd": cwd} if cwd else {}
        return self._run(list(argv), env=dict(env) if env else None, timeout=timeout, log=True,
                         dry_run=self.dry_run, **kw)

    def stream(self, argv: Sequence[str], *, cwd: str | None = None, env: Mapping[str, str] | None = None,
               timeout: float = 600.0, on_line=None, cancel=None):
        """Long side-effecting command with line callback + cancellation (exec.run_stream)."""
        self.acted.append((tuple(argv), dict(env or {})))
        fn = self._stream or _exec_stream
        return fn(list(argv), cwd=cwd, env=dict(env) if env else None, timeout=timeout, on_line=on_line,
                  cancel=cancel, dry_run=self.dry_run, log=True)

    def internal(self, description: str, fn: Callable[[], Any]) -> Any:
        """A side-effecting step done in-process (e.g. appending to .git/info/exclude): logged like
        a command, skipped under dry-run."""
        from agentree.actions import exec as ex

        self.acted.append(((f"agentree:{description}",), {}))
        if self.dry_run:
            ex.log_line(description, "DRY")
            return None
        try:
            out = fn()
        except OSError as e:
            ex.log_line(f"{description}: {e}", "ERR")
            raise
        ex.log_line(description, "0")
        return out
