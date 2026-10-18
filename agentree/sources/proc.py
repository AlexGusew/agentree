"""Process inspection. macOS (no /proc): ps, sysctl KERN_PROCARGS2, libproc, lsof. Linux: ps, /proc, lsof."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from dataclasses import dataclass, field
from typing import Iterable

from agentree.actions import exec as ex
from agentree.actions import runner

CTL_KERN = 1
KERN_ARGMAX = 8
KERN_PROCARGS2 = 49
PROC_PIDVNODEPATHINFO = 9
VNODE_INFO_SIZE = 152
MAXPATHLEN = 1024
VNODE_INFO_PATH_SIZE = VNODE_INFO_SIZE + MAXPATHLEN  # 1176
PROC_VNODEPATHINFO_SIZE = 2 * VNODE_INFO_PATH_SIZE   # 2352: pvi_cdir + pvi_rdir


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- ps

def parse_ps(text: str) -> list[tuple[int, int, str]]:
    out: list[tuple[int, int, str]] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        out.append((pid, ppid, parts[2] if len(parts) > 2 else ""))
    return out


def list_procs(timeout: float = 1.0) -> list[tuple[int, int, str]]:
    r = ex.run(["ps", "-axww", "-o", "pid=,ppid=,command="], timeout=timeout)
    return parse_ps(r.stdout) if r.ok else []


# --------------------------------------------------------------------------- sysctl argv/env

_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    return _libc


def _argmax() -> int:
    libc = _get_libc()
    mib = (ctypes.c_int * 2)(CTL_KERN, KERN_ARGMAX)
    val = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(val))
    if libc.sysctl(mib, 2, ctypes.byref(val), ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        return 1 << 20
    return max(val.value, 4096)


def parse_procargs2(buf: bytes) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Parse KERN_PROCARGS2: int32 argc, exec path\\0, \\0 padding, argv[argc]\\0..., env\\0..., \\0."""
    if len(buf) < 4:
        return None
    (argc,) = struct.unpack_from("=i", buf, 0)
    pos = 4
    end = buf.find(b"\0", pos)
    if end < 0:
        return None
    pos = end
    while pos < len(buf) and buf[pos] == 0:
        pos += 1
    argv: list[str] = []
    while len(argv) < argc and pos < len(buf):
        end = buf.find(b"\0", pos)
        if end < 0:
            end = len(buf)
        argv.append(buf[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    env: dict[str, str] = {}
    while pos < len(buf):
        end = buf.find(b"\0", pos)
        if end < 0:
            end = len(buf)
        if end == pos:
            break
        item = buf[pos:end].decode("utf-8", "replace")
        k, sep, v = item.partition("=")
        if sep and k:
            env.setdefault(k, v)
        pos = end + 1
    return tuple(argv), env


def _procfs_args_env(pid: int) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """Linux: /proc/<pid>/cmdline and /proc/<pid>/environ (the environment the process started with)."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            args = fh.read().split(b"\0")
        with open(f"/proc/{int(pid)}/environ", "rb") as fh:
            items = fh.read().split(b"\0")
    except (OSError, ValueError):
        return None
    if args[-1] == b"":                     # NUL-terminated; an empty file is a zombie / kernel thread
        args.pop()
    if not args:
        return None
    env: dict[str, str] = {}
    for item in items:
        k, sep, v = item.decode("utf-8", "replace").partition("=")
        if sep and k:
            env.setdefault(k, v)
    return tuple(a.decode("utf-8", "replace") for a in args), env


def proc_args_env(pid: int) -> tuple[tuple[str, ...], dict[str, str]] | None:
    """argv and environment of a same-user process; None if unreadable."""
    if runner.LINUX:
        return _procfs_args_env(pid)
    try:
        libc = _get_libc()
        size = _argmax()
        buf = ctypes.create_string_buffer(size)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, int(pid))
        sz = ctypes.c_size_t(size)
        if libc.sysctl(mib, 3, buf, ctypes.byref(sz), None, ctypes.c_size_t(0)) != 0:
            return None
        return parse_procargs2(buf.raw[: sz.value])
    except (OSError, ValueError, AttributeError):
        return None


KERN_PROC = 14
KERN_PROC_PID = 1
KINFO_PROC_SIZE = 648        # sizeof(struct kinfo_proc) on macOS (arm64 / x86_64)


def parse_kinfo_start(buf: bytes) -> float | None:
    """Start time from a kinfo_proc: kp_proc.p_un.__p_starttime (struct timeval at offset 0)."""
    if len(buf) < 16:
        return None
    sec, usec = struct.unpack_from("=qi", buf, 0)
    if sec <= 0 or not 0 <= usec < 1_000_000:
        return None
    return sec + usec / 1e6


def start_time(pid: int) -> float | None:
    """Process start time (unix seconds, µs precision) via sysctl KERN_PROC_PID; None if the
    process does not exist. (pid, start_time) identifies a process across pid reuse."""
    if runner.LINUX:
        return runner.proc_start_time(pid)
    try:
        libc = _get_libc()
        mib = (ctypes.c_int * 4)(CTL_KERN, KERN_PROC, KERN_PROC_PID, int(pid))
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
            return None
        return parse_kinfo_start(buf.raw[: size.value])
    except (OSError, ValueError, AttributeError, OverflowError):
        return None


def start_times(pids: Iterable[int]) -> dict[int, float]:
    """{pid: start time} for the pids that exist (one sysctl each, ~µs)."""
    out: dict[int, float] = {}
    for pid in pids:
        t = start_time(pid)
        if t is not None:
            out[pid] = t
    return out


def get_procs(pids: Iterable[int], ppids: dict[int, int] | None = None) -> list[Proc]:
    out = []
    for pid in pids:
        ae = proc_args_env(pid)
        if ae is None:
            continue
        out.append(Proc(pid, (ppids or {}).get(pid, 0), ae[0], ae[1]))
    return out


# --------------------------------------------------------------------------- cwd

_libproc = None


def _get_libproc():
    global _libproc
    if _libproc is None:
        path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        try:
            _libproc = ctypes.CDLL(path, use_errno=True)
        except OSError:
            _libproc = _get_libc()  # proc_pidinfo also lives in libSystem
    return _libproc


def _cwd_libproc(pid: int) -> str | None:
    try:
        lp = _get_libproc()
        buf = ctypes.create_string_buffer(PROC_VNODEPATHINFO_SIZE)
        n = lp.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(PROC_PIDVNODEPATHINFO), ctypes.c_uint64(0),
                            buf, ctypes.c_int(PROC_VNODEPATHINFO_SIZE))
    except (OSError, AttributeError):
        return None
    if n != PROC_VNODEPATHINFO_SIZE:
        return None
    raw = buf.raw[VNODE_INFO_SIZE:VNODE_INFO_SIZE + MAXPATHLEN]
    path = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    return path or None


def _cwd_procfs(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{int(pid)}/cwd") or None
    except (OSError, ValueError):
        return None


def parse_lsof_fpn(text: str) -> dict[int, list[str]]:
    """Parse `lsof -F pn` output into {pid: [name, ...]}."""
    out: dict[int, list[str]] = {}
    pid: int | None = None
    for line in text.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(val)
            except ValueError:
                pid = None
            if pid is not None:
                out.setdefault(pid, [])
        elif tag == "n" and pid is not None:
            out[pid].append(val)
    return out


def _cwd_lsof(pids: list[int], timeout: float) -> dict[int, str]:
    if not pids:
        return {}
    r = ex.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-p", ",".join(map(str, pids))], timeout=timeout)
    return {pid: names[0] for pid, names in parse_lsof_fpn(r.stdout).items() if names}


def proc_cwd(pids: Iterable[int], timeout: float = 1.0, use_libproc: bool = True) -> dict[int, str]:
    pids = list(dict.fromkeys(int(p) for p in pids))
    out: dict[int, str] = {}
    missing: list[int] = []
    for pid in pids:
        cwd = (_cwd_procfs(pid) if runner.LINUX else _cwd_libproc(pid)) if use_libproc else None
        if cwd:
            out[pid] = cwd
        else:
            missing.append(pid)
    if missing:
        out.update(_cwd_lsof(missing, timeout))
    return out


# --------------------------------------------------------------------------- listening ports

def parse_lsof_listen(text: str) -> dict[int, list[int]]:
    """Parse `lsof -nP -iTCP -sTCP:LISTEN -Fpn` output -> {pid: sorted unique ports}."""
    out: dict[int, list[int]] = {}
    for pid, names in parse_lsof_fpn(text).items():
        ports: list[int] = []
        for n in names:
            _, sep, port = n.rpartition(":")
            if sep and port.isdigit():
                p = int(port)
                if p not in ports:
                    ports.append(p)
        out[pid] = sorted(ports)
    return out


def listening_ports(pids: Iterable[int], timeout: float = 1.0) -> dict[int, list[int]]:
    pids = list(dict.fromkeys(int(p) for p in pids))
    if not pids:
        return {}
    r = ex.run(["lsof", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fpn", "-p", ",".join(map(str, pids))],
               timeout=timeout)
    return parse_lsof_listen(r.stdout)
