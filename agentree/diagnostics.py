"""Provider-neutral diagnostics primitives shared by `agentree doctor` and providers' doctor jobs.

`Check` is one report line; `Probe` is the injectable system access every check goes through
(tests pass fakes). Copied from agentree.doctor (which switches to importing from here); provider-
specific probes (opencode HTTP/SSE, Claude paths) live with their providers.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, NamedTuple

from agentree.actions import exec as ex
from agentree.sources import git as gitmod
from agentree.sources import kitty as kittymod
from agentree.sources import proc as procmod
from agentree.sources import zellij as zmod

__all__ = ["Check", "Probe", "CMD_TIMEOUT", "parse_version", "fmt_version", "strip_ansi", "err_text",
           "expand", "tilde", "check_tool", "writable_or_creatable", "read_text", "pid_alive",
           "proc_argv", "list_journals"]

CMD_TIMEOUT = 1.0


class Check(NamedTuple):
    section: str
    level: str          # "ok" | "warn" | "fail"
    text: str


# --------------------------------------------------------------------------- system access

def list_journals() -> list[dict]:
    """Leftover spawn journals [{"run_id", "path", "data"}]: state.list_journals() when available,
    else a glob of <state>/journal/*.json."""
    try:
        from agentree import state as st
        fn = getattr(st, "list_journals", None)
    except ImportError:
        fn = None
    if fn is not None:
        return list(fn())
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(ex.state_dir(), "journal", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        out.append({"run_id": os.path.basename(path)[:-5], "path": path, "data": data})
    return out


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def proc_argv(pid: int) -> tuple[str, ...] | None:
    ae = procmod.proc_args_env(pid)
    return ae[0] if ae else None


@dataclass
class Probe:
    """Injectable, provider-neutral system access for every check."""
    run: Callable[..., Any] = ex.run                       # run(argv, timeout=, env=) -> exec.Result
    which: Callable[[str], str | None] = ex.which          # tool lookup (PATH + well-known dirs)
    path_which: Callable[[str, str], str | None] = lambda name, path: shutil.which(name, path=path)
    kitty_sockets: Callable[[str | None], list[str]] = kittymod.sockets
    is_owned_socket: Callable[[str], bool] = kittymod.is_owned_socket
    zellij_dirs: Callable[[list[str] | None], list[str]] = zmod.socket_dirs
    has_socket: Callable[[str, str], bool] = zmod.has_socket   # has_socket(socket dir, session name)
    discover_repos: Callable[[list[str]], list[str]] = gitmod.discover_repos
    read_text: Callable[[str], str | None] = read_text
    glob: Callable[[str], list[str]] = glob.glob
    exists: Callable[[str], bool] = os.path.exists
    is_exec: Callable[[str], bool] = lambda p: os.path.isfile(p) and os.access(p, os.X_OK)
    writable_dir: Callable[[str], bool] = lambda p: os.path.isdir(p) and os.access(p, os.W_OK | os.X_OK)
    state_dir: Callable[[], str] = ex.state_dir
    list_journals: Callable[[], list[dict]] = list_journals
    pid_alive: Callable[[int], bool] = pid_alive
    start_time: Callable[[int], float | None] = procmod.start_time
    proc_argv: Callable[[int], tuple[str, ...] | None] = lambda pid: proc_argv(pid)
    # generic process primitives (providers' doctor jobs discover their own processes with these)
    list_procs: Callable[..., list[tuple[int, int, str]]] = procmod.list_procs   # list_procs(timeout=)
    proc_args_env: Callable[[int], tuple[tuple[str, ...], dict[str, str]] | None] = procmod.proc_args_env
    proc_cwd: Callable[..., dict[int, str]] = procmod.proc_cwd                   # proc_cwd(pids, timeout=)
    listening_ports: Callable[..., dict[int, list[int]]] = procmod.listening_ports  # (pids, timeout=)
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    python_version: tuple[int, ...] = field(default_factory=lambda: tuple(sys.version_info[:3]))


# --------------------------------------------------------------------------- pure helpers

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_VER = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text or "")


def parse_version(text: str) -> tuple[int, ...] | None:
    t = _ANSI.sub("", text or "")
    m = re.search(r"\bversion=v?(\d+(?:\.\d+)+)", t)        # lazygit: "commit=…, version=0.44.1, …"
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    m = _VER.search(t)
    if not m:
        return None
    return tuple(int(x) for x in m.groups() if x is not None)


def fmt_version(v: tuple[int, ...] | None) -> str:
    return ".".join(map(str, v)) if v else "?"


def err_text(r: Any) -> str:
    """One-line failure reason of an exec.Result."""
    if getattr(r, "timed_out", False):
        return "timed out"
    txt = _ANSI.sub("", (r.stderr or r.stdout or "")).strip()
    return txt.splitlines()[0][:160] if txt else f"exit {r.code}"


def expand(p: str, probe: Probe) -> str:
    """~ expansion against the probe's home (tests use a fake home)."""
    if p == "~" or p.startswith("~/"):
        return probe.home.rstrip("/") + p[1:]
    return os.path.expanduser(p)


def tilde(p: str, home: str) -> str:
    return "~" + p[len(home):] if home and (p == home or p.startswith(home.rstrip("/") + "/")) else p


def check_tool(name: str, probe: Probe, *, missing: str, minimum: tuple[int, ...] | None = None,
               missing_text: str | None = None, binary: str | None = None,
               section: str = "versions") -> list[Check]:
    S = section
    exe = binary or probe.which(name)
    if not exe:
        return [Check(S, missing, missing_text or f"{name}: not found")]
    r = probe.run([exe, "--version"], timeout=CMD_TIMEOUT)
    if r.code == 127:
        return [Check(S, missing, missing_text or f"{name}: not found")]
    if not r.ok:
        return [Check(S, missing, f"{name}: `{name} --version` failed: {err_text(r)}")]
    v = parse_version(r.stdout + "\n" + r.stderr)
    if v is None:
        return [Check(S, "warn", f"{name}: unrecognised version output {(_ANSI.sub('', r.stdout).strip()[:60])!r}")]
    if minimum and v[:len(minimum)] < minimum:
        return [Check(S, "fail", f"{name} {fmt_version(v)} (need >= {fmt_version(minimum)})")]
    return [Check(S, "ok", f"{name} {fmt_version(v)}")]


def writable_or_creatable(path: str, probe: Probe) -> tuple[bool, str]:
    """(ok, how) for a directory agentree writes into: writable if it exists, else its nearest
    existing ancestor must be writable."""
    if probe.exists(path):
        return probe.writable_dir(path), "writable" if probe.writable_dir(path) else "not a writable directory"
    parent = os.path.dirname(path.rstrip("/"))
    while parent and parent != "/" and not probe.exists(parent):
        parent = os.path.dirname(parent)
    ok = probe.writable_dir(parent or "/")
    return ok, "will be created" if ok else f"cannot be created ({tilde(parent or '/', probe.home)} not writable)"


# old doctor-private names (doctor.py switches to importing these)
_ANSI_RE = _ANSI
_err = err_text
_expand = expand
_tilde = tilde
_writable_or_creatable = writable_or_creatable
_read_text = read_text
_pid_alive = pid_alive
_proc_argv = proc_argv
_list_journals = list_journals
