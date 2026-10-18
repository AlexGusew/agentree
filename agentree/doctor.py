"""`agentree doctor`: read-only environment checks with a readable OK / WARN / FAIL report.

Every system access goes through `Probe` (agentree.diagnostics) so tests can inject fakes. Check groups
run in parallel on daemon threads (`_par`) under one wall deadline; a group that does not finish in
time is reported as FAIL. Nothing here writes to the system (no provider housekeeping either).

Sections, in order: versions, kitty, zellij, one per agent provider (registry order: the jobs
each provider returns from doctor_jobs(cfg, probe)), config. Core code knows no provider.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
import sys
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence, TextIO

from agentree.config import Config
from agentree.diagnostics import (  # noqa: F401 - re-exported (old doctor API)
    CMD_TIMEOUT,
    Check,
    Probe,
    _err,
    _expand,
    _list_journals,
    _pid_alive,
    _proc_argv,
    _read_text,
    _tilde,
    _writable_or_creatable,
    check_tool,
    fmt_version,
    parse_version,
)
from agentree.sources import _par
from agentree.sources import kitty as kittymod
from agentree.sources import zellij as zmod

if TYPE_CHECKING:
    from agentree.providers.base import DoctorJob, Provider

CORE_SECTIONS = ("versions", "kitty", "zellij")
LAST_SECTIONS = ("config",)
MARK = {"ok": "✓", "warn": "!", "fail": "✗"}
WORD = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}
DEADLINE_S = 1.8
ZELLIJ_MIN = (0, 45)
PYTHON_MIN = (3, 11)
BUILTIN_LAYOUTS = frozenset({"default", "compact", "classic", "strider", "welcome", "disable-status-bar"})
REMOTE_CONTROL_OK = {"yes", "y", "true", "socket", "socket-only", "password"}


def sections(providers: Sequence[Any] = ()) -> tuple[str, ...]:
    """Report section order: versions, kitty, zellij, the provider ids (registry order), config."""
    return (*CORE_SECTIONS, *(p.id for p in providers), *LAST_SECTIONS)


# --------------------------------------------------------------------------- pure helpers

def _strip_kdl_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)(^|[ \t])//.*$", r"\1", text)


_RUN_RE = re.compile(r'\bRun\s+"([^"]*)"')
_BIND_RE = re.compile(r'\bbind\s+((?:"[^"]*"\s*)+)\{')


def find_agentree_keybinds(text: str) -> list[tuple[str, str]]:
    """[(keys, command)] for every keybinding whose `Run "<cmd>"` command is agentree."""
    t = _strip_kdl_comments(text)
    out: list[tuple[str, str]] = []
    binds = list(_BIND_RE.finditer(t))
    for m in _RUN_RE.finditer(t):
        cmd = m.group(1)
        if os.path.basename(cmd.split()[0] if cmd.split() else "") != "agentree":
            continue
        before = [b for b in binds if b.start() < m.start()]
        keys = ", ".join(re.findall(r'"([^"]*)"', before[-1].group(1))) if before else "?"
        out.append((keys, cmd))
    return out


def kdl_string_option(text: str, name: str) -> str | None:
    m = None
    for m in re.finditer(rf'(?m)^\s*{re.escape(name)}\s+"([^"]*)"', _strip_kdl_comments(text)):
        pass
    return m.group(1) if m else None


def kitty_conf_settings(main: str, read_text: Callable[[str], str | None],
                        globber: Callable[[str], list[str]] = glob.glob,
                        environ: Mapping[str, str] | None = None, keys: tuple[str, ...] = (
                            "allow_remote_control", "listen_on")) -> tuple[dict[str, str], list[str], list[str]]:
    """(last value per key, files read, problems) following `include` / `globinclude`
    (best effort, relative to the main config's directory; `envinclude` is ignored)."""
    env = dict(environ or {})
    base = os.path.dirname(main)
    vals: dict[str, str] = {}
    files: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()

    def expand(p: str) -> str:
        p = os.path.expanduser(p)
        p = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), p)
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))

    def visit(path: str, depth: int) -> None:
        if path in seen or depth > 8:
            return
        seen.add(path)
        text = read_text(path)
        if text is None:
            problems.append(f"cannot read {path}")
            return
        files.append(path)
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition(" ")
            val = val.strip()
            if key == "include" and val:
                visit(expand(val), depth + 1)
            elif key == "globinclude" and val:
                for p in sorted(globber(expand(val))):
                    visit(p, depth + 1)
            elif key in keys:
                vals[key] = val
    visit(main, 0)
    return vals, files, problems


def listen_on_matches(listen_on: str, socket_glob: str) -> bool:
    """Does kitty's actual socket for `listen_on` (unix:<path>, `-<pid>` appended unless the
    path has {kitty_pid}) match the configured socket glob?"""
    if not listen_on.startswith("unix:") or listen_on.startswith("unix:@"):
        return False
    path = os.path.expanduser(listen_on[len("unix:"):])
    actual = path.replace("{kitty_pid}", "12345") if "{kitty_pid}" in path else f"{path}-12345"
    return fnmatch.fnmatch(actual, os.path.expanduser(socket_glob))


# --------------------------------------------------------------------------- checks

def check_python(probe: Probe) -> list[Check]:
    v = probe.python_version
    lvl = "ok" if tuple(v[:2]) >= PYTHON_MIN else "fail"
    return [Check("versions", lvl, f"python {fmt_version(v)}" + ("" if lvl == "ok" else
                                                                  f" (need >= {fmt_version(PYTHON_MIN)})"))]


def version_jobs(probe: Probe, provider_versions: Sequence[Callable[[], Sequence[Check]]] = ()
                 ) -> list[Callable[[], Sequence[Check]]]:
    """Core tool versions; the providers' own version checks (section "versions") go after
    kitty, where the agent CLI version always was."""
    kitty = probe.which("kitty") or (kittymod.KITTY_FALLBACK if probe.exists(kittymod.KITTY_FALLBACK) else None)
    return [
        lambda: check_tool("zellij", probe, missing="fail", minimum=ZELLIJ_MIN),
        lambda: check_tool("kitty", probe, missing="fail", binary=kitty),
        *provider_versions,
        lambda: check_tool("git", probe, missing="fail"),
        lambda: check_python(probe),
        lambda: check_tool("lazygit", probe, missing="warn",
                           missing_text="g (lazygit) disabled; install lazygit to enable"),
    ]


def kitty_conf_path(probe: Probe) -> str:
    env = probe.environ
    d = env.get("KITTY_CONFIG_DIRECTORY")
    if not d:
        xdg = env.get("XDG_CONFIG_HOME") or os.path.join(probe.home, ".config")
        d = os.path.join(xdg, "kitty")
    return os.path.join(_expand(d, probe), "kitty.conf")


def check_kitty(cfg: Config, probe: Probe) -> list[Check]:
    S = "kitty"
    out: list[Check] = []
    socks = probe.kitty_sockets(cfg.kitty_socket_glob)
    for p in sorted(probe.glob(_expand(cfg.kitty_socket_glob, probe))):
        if not probe.is_owned_socket(p):
            out.append(Check(S, "warn", f"ignoring {p}: not an owned live kitty socket (stale or foreign)"))
    if not socks:
        out.append(Check(S, "warn", f"no owned kitty sockets match {cfg.kitty_socket_glob} "
                                    "(jumping into kitty windows disabled)"))
    else:
        kitty = probe.which("kitty") or kittymod.KITTY_FALLBACK

        def ls(sock: str) -> Check:
            r = probe.run([kitty, "@", "--to", sock, "ls"], timeout=CMD_TIMEOUT)
            if not r.ok:
                return Check(S, "fail", f"{sock}: kitty @ ls failed: {_err(r)}")
            try:
                data = json.loads(r.stdout)
                nw = sum(len(t.get("windows") or []) for ow in data for t in ow.get("tabs") or [])
                return Check(S, "ok", f"{sock}: kitty @ ls ok ({len(data)} OS window(s), {nw} window(s))")
            except (ValueError, TypeError, AttributeError) as e:
                return Check(S, "fail", f"{sock}: kitty @ ls returned bad JSON: {e}")
        res = _par.run_all([lambda s=s: ls(s) for s in socks], workers=8,
                           deadline=time.monotonic() + CMD_TIMEOUT + 0.3, name="agentree-doctor")
        for s, o in zip(socks, res):
            out.append(o[1] if o[0] == _par.OK else Check(S, "fail", f"{s}: kitty @ ls timed out"))
    conf = kitty_conf_path(probe)
    if probe.read_text(conf) is None:
        out.append(Check(S, "fail", f"kitty.conf not found at {_tilde(conf, probe.home)}"))
        return out
    vals, files, problems = kitty_conf_settings(conf, probe.read_text, probe.glob, probe.environ)
    for p in problems:
        out.append(Check(S, "warn", f"kitty.conf include: {_tilde(p, probe.home)}"))
    arc = vals.get("allow_remote_control")
    if arc is None:
        out.append(Check(S, "fail", "kitty.conf: allow_remote_control not set (default no); set it to yes"))
    elif arc.split()[0].lower() in REMOTE_CONTROL_OK:
        out.append(Check(S, "ok", f"kitty.conf: allow_remote_control {arc}"))
    else:
        out.append(Check(S, "fail", f"kitty.conf: allow_remote_control {arc} (need yes or socket-only)"))
    lo = vals.get("listen_on")
    if lo is None:
        out.append(Check(S, "fail", "kitty.conf: listen_on not set; add `listen_on unix:/tmp/kitty.sock`"))
    elif lo.lower() == "none":
        out.append(Check(S, "fail", "kitty.conf: listen_on none; add `listen_on unix:/tmp/kitty.sock`"))
    elif listen_on_matches(lo, cfg.kitty_socket_glob):
        out.append(Check(S, "ok", f"kitty.conf: listen_on {lo}"))
    else:
        out.append(Check(S, "warn", f"kitty.conf: listen_on {lo} does not match kitty_socket_glob "
                                    f"{cfg.kitty_socket_glob}"))
    return out


def zellij_config_dir(probe: Probe) -> str:
    env = probe.environ
    if env.get("ZELLIJ_CONFIG_DIR"):
        return _expand(env["ZELLIJ_CONFIG_DIR"], probe)
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(probe.home, ".config")
    return os.path.join(xdg, "zellij")


def check_zellij_sessions(cfg: Config, probe: Probe) -> list[Check]:
    S = "zellij"
    dirs = probe.zellij_dirs(list(cfg.zellij_socket_dirs) or None)
    if not dirs:
        return [Check(S, "warn", "no zellij socket dirs found (is a zellij session running?)")]
    zj = probe.which("zellij") or "zellij"

    def ls(d: str):
        # without our own zellij client env (see zellij.CLIENT_ENV)
        return probe.run(zmod.list_sessions_argv(zj), env={"ZELLIJ_SOCKET_DIR": d}, timeout=CMD_TIMEOUT)
    res = _par.run_all([lambda d=d: ls(d) for d in dirs], workers=8,
                       deadline=time.monotonic() + CMD_TIMEOUT + 0.3, name="agentree-doctor")
    out: list[Check] = []
    per_dir: dict[str, list[zmod.ParsedSession]] = {}
    for d, o in zip(dirs, res):
        r = _par.value(o)
        shown = _tilde(d, probe.home)
        if r is None:
            out.append(Check(S, "warn", f"socket dir {shown}: list-sessions timed out"))
            continue
        text = (r.stdout or "") + "\n" + (r.stderr or "")
        if not r.ok and "No active zellij sessions" not in text:
            out.append(Check(S, "warn", f"socket dir {shown}: list-sessions failed: {_err(r)}"))
            continue
        # a session listed live without a socket in this dir is not live here
        parsed = zmod.verify_live({d: zmod.parse_list_sessions(r.stdout or "")}, probe.has_socket)[d]
        per_dir[d] = parsed
        live = [p.name for p in parsed if not p.exited]
        names = ", ".join(live[:6]) + (f", +{len(live) - 6}" if len(live) > 6 else "")
        out.append(Check(S, "ok", f"socket dir {shown}: {len(live)} live session(s)" + (f": {names}" if live else "")))
    for name, ds in sorted(zmod.live_duplicates(per_dir).items()):
        out.append(Check(S, "warn", f"session {name!r} is live in {len(ds)} socket dirs "
                                    f"({', '.join(_tilde(d, probe.home) for d in ds)}); it cannot be addressed by name"))
    return out


def check_zellij_config(cfg: Config, probe: Probe) -> list[Check]:
    S = "zellij"
    out: list[Check] = []
    cdir = zellij_config_dir(probe)
    cfile = _expand(probe.environ.get("ZELLIJ_CONFIG_FILE") or os.path.join(cdir, "config.kdl"), probe)
    text = probe.read_text(cfile)
    shown = _tilde(cfile, probe.home)
    if text is None:
        out.append(Check(S, "warn", f"{shown} not found: no agentree keybind (add bind \"Alt w\" {{ Run \"agentree\" ... }})"))
        text = ""
    else:
        binds = find_agentree_keybinds(text)
        if not binds:
            out.append(Check(S, "warn", f"no keybind runs agentree in {shown} (add bind \"Alt w\" {{ Run \"agentree\" ... }})"))
        for keys, cmd in binds:
            exp = _expand(cmd.split()[0], probe)
            if os.path.isabs(exp) and not probe.is_exec(exp):
                out.append(Check(S, "warn", f"keybind {keys} runs {cmd}, which is not an executable file"))
            else:
                out.append(Check(S, "ok", f"keybind {keys} runs {_tilde(cmd, probe.home)}"))
    ldir = kdl_string_option(text, "layout_dir")
    ldir = _expand(ldir, probe) if ldir else os.path.join(cdir, "layouts")
    names = list(dict.fromkeys([*cfg.layouts, *([cfg.tab_layout] if cfg.tab_layout else [])]))
    found: list[str] = []
    for name in names:
        if "/" in name or name.endswith(".kdl"):
            p = _expand(name, probe)
            if probe.exists(p):
                found.append(f"{name} (file)")
            else:
                out.append(Check(S, "warn", f"layout {name!r}: file not found"))
            continue
        p = os.path.join(ldir, f"{name}.kdl")
        if probe.exists(p):
            found.append(f"{name} ({_tilde(p, probe.home)})")
        elif name in BUILTIN_LAYOUTS:
            found.append(f"{name} (built-in)")
        else:
            lvl = "fail" if name == cfg.tab_layout else "warn"
            out.append(Check(S, lvl, f"layout {name!r} not found in {_tilde(ldir, probe.home)} and not built-in"))
    if found:
        out.append(Check(S, "ok", "layouts: " + ", ".join(found)))
    return out


def check_config(cfg: Config, probe: Probe) -> list[Check]:
    S = "config"
    h = probe.home
    out: list[Check] = []
    if cfg.path:
        out.append(Check(S, "ok", f"config {_tilde(cfg.path, h)}"))
    else:
        from agentree.paths import config_path

        p = config_path(probe.environ, h)
        out.append(Check(S, "ok", f"no config file ({_tilde(p, h)}); using defaults"))
    for w in cfg.warnings:
        out.append(Check(S, "warn", f"config: {w}"))
    try:
        repos = probe.discover_repos(list(cfg.repo_roots))
    except Exception as e:  # noqa: BLE001
        repos = []
        out.append(Check(S, "warn", f"repo_roots: {e}"))
    roots = ", ".join(cfg.repo_roots)
    if repos:
        out.append(Check(S, "ok", f"repo_roots {roots} -> {len(repos)} repo(s)"))
    else:
        out.append(Check(S, "warn", f"repo_roots {roots} -> no git repositories"))
    try:
        sample = cfg.worktree_path(repos[0] if repos else os.path.join(h, "Projects/repo"), "<slug>")
        out.append(Check(S, "ok", f"worktree template {cfg.worktree_template} (e.g. {_tilde(sample, h)})"))
    except (KeyError, IndexError, ValueError) as e:
        out.append(Check(S, "fail", f"worktree template {cfg.worktree_template!r} is invalid: {e!r}"))
    sd = probe.state_dir()
    if probe.exists(sd):
        if probe.writable_dir(sd):
            out.append(Check(S, "ok", f"state dir {_tilde(sd, h)} writable"))
        else:
            out.append(Check(S, "fail", f"state dir {_tilde(sd, h)} is not a writable directory"))
    else:
        parent = os.path.dirname(sd.rstrip("/"))
        while parent and parent != "/" and not probe.exists(parent):
            parent = os.path.dirname(parent)
        if probe.writable_dir(parent or "/"):
            out.append(Check(S, "ok", f"state dir {_tilde(sd, h)} (will be created)"))
        else:
            out.append(Check(S, "fail", f"state dir {_tilde(sd, h)} cannot be created ({parent} not writable)"))
    binp = os.path.join(h, "bin", "agentree")
    installed = probe.is_exec(binp)
    on_path = probe.path_which("agentree", probe.environ.get("PATH", ""))
    if installed:
        out.append(Check(S, "ok", "~/bin/agentree installed"))
    elif not on_path:                       # installed elsewhere (pipx, uv tool) is fine
        out.append(Check(S, "warn", "~/bin/agentree not installed (python3 scripts/build.py)"))
    if on_path and installed and os.path.realpath(on_path) == os.path.realpath(binp):
        out.append(Check(S, "ok", "agentree on PATH"))
    elif on_path:
        out.append(Check(S, "warn" if installed else "ok", f"agentree on PATH is {_tilde(on_path, h)}"
                         + (" (not ~/bin/agentree)" if installed else "")))
    else:
        out.append(Check(S, "warn", "agentree is not on PATH (add ~/bin to PATH; the zellij keybind may use an absolute path)"))
    return out


def _spawn_running(probe: Probe, pid: int, data: dict) -> bool:
    """Is the journal's writer still running? With a recorded process start time ("start"/
    "pid_start"), the pid must be alive with that start time (pid reuse); without one, the pid
    must be alive and be a agentree process (an argv element named agentree)."""
    if not probe.pid_alive(pid):
        return False
    rec = data.get("start", data.get("pid_start"))
    if isinstance(rec, (int, float)) and not isinstance(rec, bool):
        actual = probe.start_time(pid)
        return actual is not None and abs(actual - float(rec)) < 0.01
    argv = probe.proc_argv(pid) or ()
    return any(os.path.basename(a) == "agentree" or a == "agentree" for a in argv)


def check_journals(probe: Probe) -> list[Check]:
    """Spawn journals left in <state>/journal: a spawn that crashed or was killed mid-way."""
    S = "config"
    try:
        items = probe.list_journals()
    except Exception as e:  # noqa: BLE001 - reported
        return [Check(S, "warn", f"cannot list spawn journals: {e}")]
    if not items:
        return [Check(S, "ok", "no leftover spawn journals")]
    out: list[Check] = []
    for j in items:
        path = _tilde(str(j.get("path") or j.get("run_id") or "?"), probe.home)
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        what = ", ".join(f"{k} {_tilde(data[k], probe.home)}" for k in ("branch", "worktree")
                         if isinstance(data.get(k), str))
        pid = data.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid != os.getpid() and _spawn_running(probe, pid, data):
            out.append(Check(S, "ok", f"spawn in progress (pid {pid}): {path}"))
            continue
        out.append(Check(S, "warn", f"interrupted spawn; inspect and clean up: {path}"
                                    + (f" ({what})" if what else "")))
    return out


# --------------------------------------------------------------------------- runner

def load_providers() -> tuple[list[Any], list[Check]]:
    """The registered providers (registry order); one that cannot be loaded is a FAIL line in
    its own section instead of breaking the whole report."""
    from agentree.providers import registry

    provs: list[Any] = []
    bad: list[Check] = []
    for s in registry.specs():
        try:
            provs.append(registry.get(s.id))
        except Exception as e:  # noqa: BLE001 - reported
            bad.append(Check(s.id, "fail", f"{s.id}: cannot load provider {s.module}: {type(e).__name__}: {e}"))
    return provs, bad


def provider_jobs(cfg: Config, probe: Probe, providers: Sequence[Provider]) -> tuple[list[DoctorJob], list[Check]]:
    """Every provider's doctor jobs, in registry order (building them must not do the work)."""
    jobs: list[DoctorJob] = []
    bad: list[Check] = []
    for prov in providers:
        try:
            jobs.extend(prov.doctor_jobs(cfg, probe))
        except Exception as e:  # noqa: BLE001 - reported
            bad.append(Check(prov.id, "fail", f"{prov.id} checks crashed: {type(e).__name__}: {e}"))
    return jobs, bad


def collect(cfg: Config, probe: Probe | None = None, deadline_s: float = DEADLINE_S, *,
            providers: Sequence[Provider] | None = None) -> list[Check]:
    """All checks, sorted by section (stable within one). `providers` (tests) defaults to the
    registry's."""
    probe = probe or Probe()
    out: list[Check] = []
    if providers is None:
        provs, bad = load_providers()
        out.extend(bad)
    else:
        provs = list(providers)
    pjobs, bad = provider_jobs(cfg, probe, provs)
    out.extend(bad)
    pver = [j.run for j in pjobs if j.section == "versions"]
    jobs: list[tuple[str, Callable[[], Sequence[Check]]]] = [("versions", j) for j in version_jobs(probe, pver)]
    jobs += [
        ("kitty", lambda: check_kitty(cfg, probe)),
        ("zellij", lambda: check_zellij_sessions(cfg, probe)),
        ("zellij", lambda: check_zellij_config(cfg, probe)),
    ]
    jobs += [(j.section, j.run) for j in pjobs if j.section != "versions"]
    jobs += [
        ("config", lambda: check_config(cfg, probe)),
        ("config", lambda: check_journals(probe)),
    ]
    res = _par.run_all([j for _, j in jobs], deadline=time.monotonic() + deadline_s, name="agentree-doctor")
    for (sec, _), o in zip(jobs, res):
        if o[0] == _par.OK:
            out.extend(o[1])
        elif o[0] == _par.PENDING:
            out.append(Check(sec, "fail", f"{sec} check timed out"))
        else:
            out.append(Check(sec, "fail", f"{sec} check crashed: {type(o[1]).__name__}: {o[1]}"))
    order = {s: i for i, s in enumerate(sections(provs))}
    return sorted(out, key=lambda c: order.get(c.section, len(order)))   # stable within a section


def format_report(checks: list[Check], elapsed_s: float | None = None) -> str:
    lines = ["agentree doctor"]
    sec = None
    for c in checks:
        if c.section != sec:
            sec = c.section
            lines.append("")
            lines.append(sec)
        lines.append(f"  {MARK[c.level]} {WORD[c.level]:<4}  {c.text}")
    n = {k: sum(1 for c in checks if c.level == k) for k in MARK}
    tail = f"{n['ok']} ok, {n['warn']} warning(s), {n['fail']} failure(s)"
    if elapsed_s is not None:
        tail += f" in {elapsed_s:.2f}s"
    lines += ["", tail]
    return "\n".join(lines) + "\n"


def run_doctor(cfg: Config, *, out: TextIO = sys.stdout, probe: Probe | None = None,
               deadline_s: float = DEADLINE_S, providers: Sequence[Provider] | None = None) -> int:
    """Print the report; 0 if no FAIL, else 1."""
    t0 = time.monotonic()
    checks = collect(cfg, probe, deadline_s, providers=providers)
    out.write(format_report(checks, time.monotonic() - t0))
    out.flush()
    return 1 if any(c.level == "fail" for c in checks) else 0


__all__ = ["Check", "Probe", "collect", "format_report", "run_doctor", "find_agentree_keybinds",
           "kitty_conf_settings", "listen_on_matches", "parse_version"]
