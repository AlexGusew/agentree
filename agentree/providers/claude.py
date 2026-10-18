"""Claude Code provider: discovery of running `claude` processes, their session and status,
the hooks agentree installs, and launching.

Claude Code has no HTTP API like opencode. agentree launches it with
`--settings <state>/claude/settings.json --session-id <uuid>`; that settings file adds command
hooks that run `agentree claude-hook`. Each hook call reads the hook JSON on stdin and writes the
session's status record to `<state>/claude/status/<session_id>.json` (atomic replace):

  {"v": 1, "session_id", "status": busy|idle|needs_input|ended, "event", "ts" (unix s),
   "since" (unix s the status started), "cwd", "transcript_path", "pid" (claude pid or null),
   "zellij_session", "zellij_pane" (int|null), "message" (notification text|null),
   "last_message" (Stop: last assistant text|null)}

Hooks merge with the user's own settings (Claude merges hook lists across settings sources), so
user hooks keep working. The hook never fails the Claude session: every error is swallowed and
the exit code is 0.

Per running process (see `fetch`):

Session id, first match wins:
  a) `--session-id <uuid>` in argv (agentree launches claude that way) — unless a hook record of this
     same process (pid or zellij pane match, newer than the argv session's own record) names
     another session: /clear and /resume switch sessions inside one process;
  b) a hook status record (<state>/claude/status/<sid>.json) whose pid — or zellij session+pane —
     is this process's, written after the process started (most recent ts);
  c) Claude's own registry `<claude_dir>/sessions/<pid>.json` (unofficial schema: sessionId, cwd,
     status busy|idle, name — parsed defensively);
  d) the newest transcript `<claude_dir>/projects/<encoded cwd>/*.jsonl` modified after the
     process started and not claimed by another process (encoding: every non-alphanumeric
     character becomes '-'; names longer than 200 chars are matched by prefix).

Status, first available wins (Agent.status_source says which; status_quality "exact" for hooks,
"approximate" for registry / transcript):
  hooks       the hook record (busy | idle | needs_input; idle > 30 min -> done). Records of
              ended sessions ("SessionEnd" / "ended") and records older than the process are ignored.
              Esc interrupts (also denying a permission prompt) send no Stop hook: a busy /
              needs_input record is overridden to idle when the transcript, modified after the
              status began, ends with "[Request interrupted…"
  registry    the registry status busy | idle
  transcript  tail heuristic on the last main-chain (non-sidechain) record: assistant end_turn ->
              idle; a user prompt, a tool result or an assistant tool_use -> busy, but only if the
              transcript changed in the last BUSY_WINDOW_S (else unknown). Approximate.

Title: latest custom-title > ai-title > summary record > registry name > first user prompt.
Context: last main-chain assistant usage (input + cache read + cache creation); the limit is
1M for models with "[1m]" in the id or of the 4.7+ / 5.x / fable families, else 200k.

A process's own $CLAUDE_CONFIG_DIR (from its environment) selects its registry / transcript root.

Transcripts are read from the end (TAIL_BYTES) plus a head chunk (HEAD_BYTES) for the first
prompt; parsed summaries are cached by (path, mtime, size). Nothing in fetch raises.
"""
from __future__ import annotations

import datetime as _dt
import glob as _glob
import json
import os
import re
import shlex
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, Literal, Mapping, Sequence

from agentree.providers.base import (DemoAgent, DemoContext, DoctorJob, FetchContext, FetchResult, Launch,
                                LaunchRequest, ProviderSpec, RefreshPolicy, SetupStep, Watcher)
from agentree.sources import proc as procmod
from agentree.types import Agent, AgentStatus, SourceWarning, ZPane

if TYPE_CHECKING:
    from agentree.actions.runner import Runner
    from agentree.diagnostics import Check, Probe

SOURCE = "claude"
APPROXIMATE_NOTE = "Status is approximate"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
               "Notification", "Stop", "SessionEnd")
NEEDS_INPUT_NOTIFICATIONS = ("permission_prompt", "elicitation_dialog", "elicitation")
MAX_STDIN = 1 << 20
# claude subcommands (a prompt equal to one of these would run it instead); see CLAUDE_SUBCOMMANDS
SUBCOMMANDS = frozenset({"mcp", "doctor", "update", "upgrade", "install", "auth", "plugin", "plugins",
                         "setup-token", "migrate-installer", "config", "login", "logout", "agents"})
MAX_TEXT = 2000


def _state_dir() -> str:
    from agentree.paths import state_dir

    return state_dir()


def claude_dir() -> str:
    """Claude Code config root ($CLAUDE_CONFIG_DIR or ~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.expanduser(env) if env else os.path.expanduser("~/.claude")


def base_dir() -> str:
    return os.path.join(_state_dir(), "claude")


def status_dir() -> str:
    return os.path.join(base_dir(), "status")


def settings_path() -> str:
    return os.path.join(base_dir(), "settings.json")


# --------------------------------------------------------------------------- launch side

def self_command() -> list[str]:
    """argv that runs this agentree (the zipapp, or `python -m agentree` from a source checkout).
    $AGENTREE_SELF (a shell-split command) overrides."""
    env = os.environ.get("AGENTREE_SELF")
    if env:
        return shlex.split(env)
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0) and not argv0.endswith(".py") and os.access(argv0, os.X_OK):
        return [argv0]
    return [sys.executable or "python3", "-m", "agentree"]


def hook_command() -> str:
    """Shell command Claude runs for each hook (the source checkout is put on PYTHONPATH)."""
    argv = self_command()
    cmd = shlex.join([*argv, "claude-hook"])
    if argv[1:3] == ["-m", "agentree"]:
        cmd = f"PYTHONPATH={shlex.quote(import_root())} {cmd}"
    return cmd


def import_root() -> str:
    """The sys.path entry the `agentree` package was imported from: the source checkout, or the
    zipapp file itself (a zip archive works as a PYTHONPATH entry)."""
    import agentree

    pkg = os.path.dirname(os.path.abspath(agentree.__file__ or ""))
    return os.path.dirname(pkg)


def settings_doc(command: str | None = None) -> dict[str, Any]:
    cmd = command or hook_command()
    hook = [{"type": "command", "command": cmd, "timeout": 10}]
    hooks: dict[str, Any] = {}
    for ev in HOOK_EVENTS:
        entry: dict[str, Any] = {"hooks": hook}
        if ev in ("PreToolUse", "PostToolUse", "PermissionRequest", "Notification"):
            entry = {"matcher": "*", "hooks": hook}
        hooks[ev] = [entry]
    return {"hooks": hooks}


def _atomic_write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
    os.replace(tmp, path)


def write_settings(command: str | None = None, path: str | None = None) -> str:
    """(Re)write the hooks settings file (default settings_path()) if its content changed;
    returns its path."""
    path = path or settings_path()
    data = json.dumps(settings_doc(command), indent=2) + "\n"
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == data:
                return path
    except OSError:
        pass
    _atomic_write(path, data)
    return path


def new_session_id() -> str:
    return str(uuid.uuid4())


def launch_args(session_id: str | None, prompt: str | None = None, *,
                settings: str | None = None) -> list[str]:
    """Arguments appended to claude_cmd: hook settings, session id, then the optional prompt
    (a positional; `--` first when it starts with '-')."""
    args = ["--settings", settings or settings_path()]
    if session_id:
        args += ["--session-id", session_id]
    if prompt:
        if prompt.startswith("-"):
            args.append("--")
        if prompt.strip() in SUBCOMMANDS:
            prompt += " "          # `claude update` would run the subcommand, "update " is a prompt
        args.append(prompt)
    return args


# --------------------------------------------------------------------------- hook side

def _status_for(event: str, payload: Mapping[str, Any], prev: str | None) -> str | None:
    """New status for a hook event; None = keep the previous status."""
    if event == "SessionStart":
        return "idle"
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "busy"
    if event == "PermissionRequest":
        return "needs_input"
    if event == "Notification":
        nt = str(payload.get("notification_type") or "")
        if nt in NEEDS_INPUT_NOTIFICATIONS:
            return "needs_input"
        if nt == "idle_prompt":
            return "idle" if prev != "needs_input" else None     # keep needs_input and its message
        msg = str(payload.get("message") or "").lower()
        if "permission" in msg or "waiting for your input" in msg:
            return "needs_input"
        return None
    if event == "Stop":
        return "idle"
    if event == "SessionEnd":
        return "ended"
    return None


def _hook_trim(s: Any) -> str | None:
    if not isinstance(s, str) or not s.strip():
        return None
    s = s.strip()
    return s if len(s) <= MAX_TEXT else s[: MAX_TEXT - 1] + "…"


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _safe_id(sid: Any) -> str | None:
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    if not sid or len(sid) > 128 or any(not (c.isalnum() or c in "-_.") for c in sid) or sid.startswith("."):
        return None
    return sid


def status_path(session_id: str) -> str:
    return os.path.join(status_dir(), f"{session_id}.json")


def read_record(session_id: str) -> dict | None:
    sid = _safe_id(session_id)
    if sid is None:
        return None
    try:
        with open(status_path(sid), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def handle_hook(payload: Mapping[str, Any], env: Mapping[str, str], ppid: int | None,
                now: float | None = None) -> dict | None:
    """Apply one hook event; returns the record written (None if ignored)."""
    now = time.time() if now is None else now
    sid = _safe_id(payload.get("session_id"))
    event = payload.get("hook_event_name")
    if sid is None or not isinstance(event, str):
        return None
    prev = read_record(sid) or {}
    status = _status_for(event, payload, prev.get("status"))
    new_status = status or prev.get("status") or "unknown"
    since = prev.get("since") if prev.get("status") == new_status and prev.get("since") else now
    pid = _int(env.get("CLAUDE_PID")) or prev.get("pid") or ppid
    rec = {
        "v": 1,
        "session_id": sid,
        "status": new_status,
        "event": event,
        "ts": now,
        "since": since,
        "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else prev.get("cwd"),
        "transcript_path": (payload.get("transcript_path") if isinstance(payload.get("transcript_path"), str)
                            else prev.get("transcript_path")),
        "pid": pid,
        "zellij_session": env.get("ZELLIJ_SESSION_NAME") or prev.get("zellij_session"),
        "zellij_pane": _int(env.get("ZELLIJ_PANE_ID")) if env.get("ZELLIJ_PANE_ID") else prev.get("zellij_pane"),
        "message": (_hook_trim(payload.get("message")) if event == "Notification" and status == "needs_input"
                    else (prev.get("message") if new_status == "needs_input" else None)),
        "last_message": _hook_trim(payload.get("last_assistant_message")) or prev.get("last_message"),
    }
    _atomic_write(status_path(sid), json.dumps(rec))
    return rec


def read_statuses(max_age_s: float | None = None, now: float | None = None) -> dict[str, dict]:
    """All hook status records by session id (ended ones included; callers filter)."""
    out: dict[str, dict] = {}
    now = time.time() if now is None else now
    try:
        names = os.listdir(status_dir())
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = read_record(name[:-5])
        if rec is None or not isinstance(rec.get("session_id"), str):
            continue
        if max_age_s is not None and now - float(rec.get("ts") or 0) > max_age_s:
            continue
        out[rec["session_id"]] = rec
    return out


def prune(max_age_s: float = 7 * 86400, now: float | None = None) -> int:
    """Delete status records older than max_age_s; returns how many were removed."""
    now = time.time() if now is None else now
    n = 0
    try:
        names = os.listdir(status_dir())
    except OSError:
        return 0
    for name in names:
        p = os.path.join(status_dir(), name)
        try:
            if name.endswith(".tmp") or now - os.stat(p).st_mtime > max_age_s:
                os.unlink(p)
                n += 1
        except OSError:
            pass
    return n


def hook_main(stdin=None, env: Mapping[str, str] | None = None) -> int:
    """`agentree claude-hook`: never raises, always exit 0 (a failing hook must not disturb Claude)."""
    try:
        raw = (stdin or sys.stdin).read(MAX_STDIN)
        payload = json.loads(raw) if raw.strip() else {}
        if isinstance(payload, dict):
            handle_hook(payload, os.environ if env is None else env, os.getppid())
    except Exception:  # noqa: BLE001 - see docstring
        pass
    return 0


# --------------------------------------------------------------------------- claude code discovery
# Native installs run ~/.local/share/claude/versions/<ver> (argv0 usually "claude" or that path;
# the kernel comm can be the version number), old npm installs run `node .../claude-code/cli.js`.

CLAUDE_SUBCOMMANDS = frozenset({
    "mcp", "doctor", "update", "upgrade", "install", "auth", "plugin", "plugins", "setup-token",
    "migrate-installer", "config", "login", "logout", "agents",
})


def _is_claude_name(path: str) -> bool:
    base = os.path.basename(path)
    if base in ("claude", ".claude") or base.startswith(".claude-"):
        return True
    return "/claude/versions/" in path or "/ClaudeCode.app/" in path


def _claude_args(argv: tuple[str, ...] | list[str]) -> tuple[str, ...] | None:
    """Arguments after the claude executable, or None if argv is not a claude process."""
    if not argv:
        return None
    if _is_claude_name(argv[0]):
        return tuple(argv[1:])
    if os.path.basename(argv[0]) in ("node", "bun") and len(argv) > 1 and (
            argv[1].endswith("claude-code/cli.js") or argv[1].endswith("claude-code/cli.mjs")):
        return tuple(argv[2:])
    return None


def is_claude_argv(argv: tuple[str, ...] | list[str]) -> bool:
    """An interactive Claude Code session (not `claude mcp ...`, `claude -p ...`, etc.)."""
    args = _claude_args(argv)
    if args is None:
        return False
    opts = args[:args.index("--")] if "--" in args else args     # after `--`: the prompt
    if any(a in ("-p", "--print", "-v", "--version", "-h", "--help") for a in opts):
        return False
    # Flags that take a value: skip the value so it is not mistaken for a subcommand.
    takes_value = {"--settings", "--session-id", "--resume", "-r", "--name", "-n", "--model",
                   "--permission-mode", "--add-dir", "--mcp-config", "--append-system-prompt",
                   "--system-prompt", "--allowedTools", "--disallowedTools", "--agent",
                   "--fallback-model", "--output-format", "--input-format", "--setting-sources"}
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a == "--":
            return True                       # the rest is the prompt
        if a.startswith("-"):
            skip = a in takes_value
            continue
        return a not in CLAUDE_SUBCOMMANDS    # first positional: subcommand or the prompt
    return True


def claude_session_arg(argv: tuple[str, ...] | list[str]) -> str | None:
    """Value of --session-id (or --session-id=...) in a claude argv."""
    args = list(_claude_args(argv) or ())
    for i, a in enumerate(args):
        if a == "--session-id" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--session-id="):
            return a.split("=", 1)[1]
    return None


def find_claude(procs: Iterable[tuple[int, int, str]] | Iterable[procmod.Proc]) -> list[int]:
    """PIDs of interactive Claude Code processes (top-level: a claude whose parent is also a
    claude process is a nested/child session and is dropped).

    `ps` rows join argv with spaces, so a prompt argument ("update the README") would split into
    a fake subcommand: for rows whose executable is claude the real argv (KERN_PROCARGS2) is read;
    the split command line is only the fallback when it is unreadable."""
    me = {os.getpid()}
    rows: list[tuple[int, int, tuple[str, ...]]] = []
    for p in procs:
        if isinstance(p, procmod.Proc):
            rows.append((p.pid, p.ppid, p.argv))
        else:
            pid, ppid, cmd = p
            argv = tuple(cmd.split())
            if pid not in me and _claude_args(argv[:2]) is not None:
                ae = procmod.proc_args_env(pid)
                if ae and ae[0]:
                    argv = tuple(ae[0])
            rows.append((pid, ppid, argv))
    hits = {pid for pid, _pp, argv in rows
            if pid not in me and argv and is_claude_argv(argv)
            and not any("agentree" == os.path.basename(a) or a.endswith("/agentree") for a in argv[:2])}
    return [pid for pid, ppid, _a in rows if pid in hits and ppid not in hits]


# --------------------------------------------------------------------------- status constants

DONE_AFTER_S = 30 * 60
BUSY_WINDOW_S = 10 * 60          # transcript heuristic: "busy" only if written this recently
STREAM_QUIET_S = 30              # assistant text without stop_reason: busy while this fresh
TAIL_BYTES = 256 << 10
HEAD_BYTES = 64 << 10
LAST_MESSAGE_MAX = 600
TITLE_MAX = 120
MAX_ENCODED = 200
START_SLACK_S = 2.0              # clock slack when comparing file times with process start
CONTEXT_DEFAULT = 200_000
CONTEXT_1M = 1_000_000
HOOK_STATUSES = ("busy", "idle", "needs_input")


# --------------------------------------------------------------------------- pure helpers

def encode_project(path: str) -> str:
    """Claude's project dir name for a cwd: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_dirs(cwd: str | None, root: str | None = None) -> list[str]:
    """Candidate `<claude_dir>/projects/<encoded cwd>` directories (existing ones only)."""
    if not cwd:
        return []
    base = os.path.join(root or claude_dir(), "projects")
    enc = encode_project(cwd)
    if len(enc) <= MAX_ENCODED:
        d = os.path.join(base, enc)
        return [d] if os.path.isdir(d) else []
    # long paths are truncated (+ a hash suffix) by Claude: match by prefix
    return sorted(p for p in _glob.glob(os.path.join(_glob.escape(base), _glob.escape(enc[:MAX_ENCODED]) + "*"))
                  if os.path.isdir(p))


# family-major[-minor]; majors/minors are 1-2 digits so date suffixes (sonnet-4-20250514) and the
# old naming (claude-3-5-sonnet-20241022) never read as versions
_VER_RE = re.compile(r"(opus|sonnet|haiku)-(\d{1,2})(?:[-.](\d{1,2}))?(?!\d)")


def context_limit(model: str | None) -> int | None:
    """Context window for a model id: 1M for "[1m]" ids and the 4.7+ / 5.x / fable families,
    else 200k (None without a model)."""
    if not model:
        return None
    m = model.lower()
    if "[1m]" in m or "fable" in m:
        return CONTEXT_1M
    v = _VER_RE.search(m)
    if v:
        ver = (int(v.group(2)), int(v.group(3) or 0))
        if ver >= (4, 7):
            return CONTEXT_1M
    return CONTEXT_DEFAULT


def _one_line(text: str, limit: int = TITLE_MAX) -> str | None:
    for line in text.splitlines():
        line = " ".join(line.split())
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return None


def _trim(text: str, limit: int = LAST_MESSAGE_MAX) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ts(value: Any) -> float | None:
    """ISO-8601 timestamp (transcript records) -> unix seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp()


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _blocks(msg: Any) -> list[dict]:
    content = (msg or {}).get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _texts(msg: Any) -> list[str]:
    return [b["text"] for b in _blocks(msg) if b.get("type") == "text" and isinstance(b.get("text"), str)
            and b["text"].strip()]


_NOT_PROMPT = ("<command-", "<local-command", "<system-reminder", "Caveat:", "[Request interrupted")


def _prompt_text(rec: dict) -> str | None:
    """Text of a real user prompt record (not a tool result / meta / command echo), else None."""
    if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
        return None
    if rec.get("toolUseResult") is not None:
        return None
    msg = rec.get("message")
    if any(b.get("type") == "tool_result" for b in _blocks(msg)):
        return None
    texts = _texts(msg)
    if not texts:
        return None
    t = texts[0].strip()
    if t.startswith(_NOT_PROMPT):
        return None
    return t


def _parse_lines(data: bytes, partial_first: bool) -> list[dict]:
    lines = data.split(b"\n")
    if partial_first and lines:
        lines = lines[1:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue          # a partial last line (being written) or garbage
        if isinstance(rec, dict):
            out.append(rec)
    return out


def summarize_records(head: list[dict], tail: list[dict]) -> dict:
    """Summary of a transcript from its first records (`head`) and last records (`tail`, in file
    order). Keys: state ("idle" | "busy" | "stream" | None), interrupted (the last main-chain
    record is an Esc interrupt marker), titles, first_prompt, last_message, context_tokens, model,
    last_prompt_ts, last_ts, session_id."""
    out: dict[str, Any] = {"state": None, "custom_title": None, "ai_title": None, "summary": None,
                           "first_prompt": None, "last_message": None, "context_tokens": None,
                           "model": None, "last_prompt_ts": None, "last_ts": None, "session_id": None,
                           "interrupted": False}
    for rec in head:
        if out["first_prompt"] is None:
            p = _prompt_text(rec)
            if p:
                out["first_prompt"] = _one_line(p)
        if out["session_id"] is None and isinstance(rec.get("sessionId"), str):
            out["session_id"] = rec["sessionId"]
    for rec in head + tail:          # titles: latest wins (tail after head)
        typ = rec.get("type")
        if typ == "custom-title":
            t = rec.get("customTitle") or rec.get("title")
            if isinstance(t, str) and t.strip():
                out["custom_title"] = _one_line(t)
        elif typ == "ai-title":
            t = rec.get("aiTitle") or rec.get("title")
            if isinstance(t, str) and t.strip():
                out["ai_title"] = _one_line(t)
        elif typ == "summary":
            t = rec.get("summary")
            if isinstance(t, str) and t.strip():
                out["summary"] = _one_line(t)
    for rec in reversed(tail):
        typ = rec.get("type")
        if typ not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        ts = _ts(rec.get("timestamp"))
        if out["last_ts"] is None and ts is not None:
            out["last_ts"] = ts
        msg = rec.get("message")
        if not isinstance(msg, dict):
            msg = {}
        if out["state"] is None:
            out["state"] = _record_state(rec, msg)
            out["interrupted"] = typ == "user" and is_interrupt(msg)
        if typ == "user" and out["last_prompt_ts"] is None and _prompt_text(rec) is not None:
            out["last_prompt_ts"] = ts
        if typ == "assistant":
            if out["last_message"] is None:
                texts = _texts(msg)
                if texts:
                    out["last_message"] = _trim(texts[-1])
            usage = msg.get("usage")
            model = msg.get("model")
            if out["context_tokens"] is None and isinstance(usage, dict) and model != "<synthetic>":
                n = sum(int(_num(usage.get(k)) or 0) for k in
                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                if n > 0:
                    out["context_tokens"] = n
                    out["model"] = model if isinstance(model, str) else None
        if (out["state"] is not None and out["last_message"] is not None and out["context_tokens"] is not None
                and out["last_prompt_ts"] is not None):
            break
    if out["session_id"] is None:
        for rec in reversed(tail):
            if isinstance(rec.get("sessionId"), str):
                out["session_id"] = rec["sessionId"]
                break
    return out


INTERRUPT_MARK = "[Request interrupted"


def is_interrupt(msg: Any) -> bool:
    """A user message that is Claude's interrupt marker ("[Request interrupted by user]" or
    "... for tool use]", as text or as a tool_result's content)."""
    for b in _blocks(msg):
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            if b["text"].lstrip().startswith(INTERRUPT_MARK):
                return True
        elif b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, str):
                texts = [c]
            elif isinstance(c, list):
                texts = [x.get("text") for x in c if isinstance(x, dict)]
            else:
                texts = []
            if any(isinstance(t, str) and t.lstrip().startswith(INTERRUPT_MARK) for t in texts):
                return True
    return False


def _record_state(rec: dict, msg: dict) -> str:
    """'idle' | 'busy' | 'stream' (assistant text without a stop reason) for the last main-chain
    record."""
    if rec.get("type") == "user":
        if is_interrupt(msg):
            return "idle"
        if rec.get("isMeta") and not any(b.get("type") == "tool_result" for b in _blocks(msg)):
            return "idle"
        return "busy"               # a prompt or a tool result: claude is working on it
    blocks = _blocks(msg)
    stop = msg.get("stop_reason")
    if any(b.get("type") == "tool_use" for b in blocks) or stop == "tool_use":
        return "busy"
    if stop in ("end_turn", "stop_sequence", "max_tokens", "refusal"):
        return "idle"
    return "stream"


# --------------------------------------------------------------------------- transcript I/O

_cache_lock = threading.Lock()
_cache: dict[str, tuple[int, int, dict]] = {}
_CACHE_MAX = 256


def read_transcript(path: str) -> dict | None:
    """Summary (see summarize_records) of a transcript, plus "mtime"; None if unreadable.
    Reads at most HEAD_BYTES + TAIL_BYTES; cached by (mtime, size)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _cache.get(path)
    if hit is not None and hit[:2] == key:
        return hit[2]
    try:
        with open(path, "rb") as fh:
            head_raw = fh.read(min(HEAD_BYTES, st.st_size))
            if st.st_size > HEAD_BYTES:
                # from one byte before the tail start: the first (maybe partial) segment up to the
                # first newline is dropped, and is empty when the tail starts on a line boundary
                start = max(HEAD_BYTES, st.st_size - TAIL_BYTES)
                fh.seek(start - 1)
                tail_raw = fh.read(TAIL_BYTES + 1)
                partial = True
            else:
                tail_raw, partial = b"", False
    except OSError:
        return None
    # a cut-off last line in the head is dropped by _parse_lines (bad JSON)
    head = _parse_lines(head_raw, False)
    tail = _parse_lines(tail_raw, partial) if tail_raw else head
    summary = summarize_records(head, tail)
    summary["mtime"] = st.st_mtime
    summary["path"] = path
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[path] = (key[0], key[1], summary)
    return summary


def find_transcript(session_id: str | None, cwds: Iterable[str | None], root: str | None = None,
                    hint: str | None = None) -> str | None:
    """Path of a session's transcript: the hook's transcript_path, else <projects>/<enc cwd>/<sid>.jsonl,
    else any project dir holding <sid>.jsonl."""
    if hint and os.path.isfile(hint):
        return hint
    if not session_id or _safe_id(session_id) is None:
        return None
    for cwd in cwds:
        for d in project_dirs(cwd, root):
            p = os.path.join(d, f"{session_id}.jsonl")
            if os.path.isfile(p):
                return p
    base = os.path.join(root or claude_dir(), "projects")
    hits = _glob.glob(os.path.join(_glob.escape(base), "*", f"{session_id}.jsonl"))
    return hits[0] if hits else None


def newest_transcripts(cwd: str | None, since: float | None, root: str | None = None) -> list[tuple[float, str]]:
    """[(mtime, path)] of transcripts in cwd's project dir modified after `since`, newest first."""
    out: list[tuple[float, str]] = []
    for d in project_dirs(cwd, root):
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".jsonl"):
                continue
            try:
                m = e.stat().st_mtime
            except OSError:
                continue
            if since is None or m >= since - START_SLACK_S:
                out.append((m, e.path))
    out.sort(reverse=True)
    return out


def read_registry(pid: int, root: str | None = None) -> dict | None:
    """Claude's `<claude_dir>/sessions/<pid>.json` (unofficial; only well-typed fields kept)."""
    try:
        with open(os.path.join(root or claude_dir(), "sessions", f"{int(pid)}.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for k in ("sessionId", "cwd", "status", "name"):
        if isinstance(data.get(k), str) and data[k].strip():
            out[k] = data[k].strip()
    for k in ("startedAt", "updatedAt"):
        v = _num(data.get(k))
        if v is not None:
            out[k] = v / 1000 if v > 1e11 else v     # ms or s
    if "pid" in data and data.get("pid") != pid and isinstance(data.get("pid"), int):
        return None
    return out


# --------------------------------------------------------------------------- resolution

def _hook_live(rec: dict | None, start: float | None) -> bool:
    """A hook record usable for status: not ended, a known status, written after process start."""
    if not rec or rec.get("status") not in HOOK_STATUSES or rec.get("event") == "SessionEnd":
        return False
    ts = _num(rec.get("ts"))
    if ts is None:
        return False
    return start is None or ts >= start - START_SLACK_S


def _hook_owned(rec: dict, pid: int, env: dict[str, str], start: float | None) -> bool:
    """Was this hook record written by process `pid`? pid match, or the same zellij pane."""
    ts = _num(rec.get("ts"))
    if ts is None or (start is not None and ts < start - START_SLACK_S):
        return False
    if rec.get("pid") == pid:
        return True
    sname, pane = env.get("ZELLIJ_SESSION_NAME"), env.get("ZELLIJ_PANE_ID")
    return bool(sname and pane and pane.isdigit() and rec.get("zellij_session") == sname
                and rec.get("zellij_pane") == int(pane))


def resolve_session(pid: int, argv: tuple[str, ...], env: dict[str, str], start: float | None,
                    hooks: dict[str, dict], registry: dict | None) -> tuple[str | None, str | None]:
    """(session id, how) per the module docstring's a)–c); d) (transcripts) is done by _agent."""
    owned = sorted((r for r in hooks.values() if _hook_owned(r, pid, env, start)),
                   key=lambda r: _num(r.get("ts")) or 0, reverse=True)
    arg = claude_session_arg(argv)
    if arg:
        own = hooks.get(arg)
        if owned and owned[0].get("session_id") != arg:
            newer = _num(owned[0].get("ts")) or 0
            if own is None or own.get("status") == "ended" or newer > (_num(own.get("ts")) or 0):
                return owned[0]["session_id"], "hook"
        return arg, "argv"
    if owned:
        return owned[0]["session_id"], "hook"
    if registry and registry.get("sessionId"):
        return registry["sessionId"], "registry"
    return None, None


def hook_status(rec: dict, now: float, done_after_s: int = DONE_AFTER_S) -> tuple[AgentStatus, int | None]:
    st = rec.get("status")
    since = _num(rec.get("since")) or _num(rec.get("ts"))
    age = None if since is None else max(0, int(now - since))
    if st == "idle" and age is not None and age >= done_after_s:
        return "done", age
    if st in HOOK_STATUSES:
        return st, age          # type: ignore[return-value]
    return "unknown", None


def transcript_status(summary: dict, now: float, done_after_s: int = DONE_AFTER_S
                      ) -> tuple[AgentStatus, int | None]:
    mtime = _num(summary.get("mtime"))
    age = None if mtime is None else max(0, int(now - mtime))
    state = summary.get("state")
    if state == "stream":
        state = "busy" if age is not None and age < STREAM_QUIET_S else "idle"
    if state == "idle":
        return ("done" if age is not None and age >= done_after_s else "idle"), age
    if state == "busy":
        if age is None or age > BUSY_WINDOW_S:
            return "unknown", None
        lp = _num(summary.get("last_prompt_ts"))
        return "busy", (max(0, int(now - lp)) if lp is not None and lp <= now else age)
    return "unknown", None


def interrupted_after(summary: dict, rec: dict) -> bool:
    """The transcript ends with an interrupt marker written after the record's status began.

    Compared with `since` (else `ts`), not `ts`: a later Notification idle_prompt rewrites the
    record (new ts) but keeps needs_input and its `since`, while the user had already denied the
    permission with Esc. A new prompt after the interrupt appends a user record, so the marker is
    no longer last and the hook status stands."""
    mt = _num(summary.get("mtime"))
    began = _num(rec.get("since")) or _num(rec.get("ts"))
    return bool(summary.get("interrupted")) and mt is not None and began is not None and mt > began


def process_root(env: dict[str, str] | None, default: str | None = None) -> str:
    """Claude config root of one process: its own $CLAUDE_CONFIG_DIR, else `default` (agentree's
    environment, see claude_dir)."""
    v = (env or {}).get("CLAUDE_CONFIG_DIR")
    if v and v.strip():
        return os.path.expanduser(v.strip())
    return default or claude_dir()


# --------------------------------------------------------------------------- per agent

def _agent(pid: int, argv: tuple[str, ...], env: dict[str, str], cwd: str | None, start: float | None,
           hooks: dict[str, dict], root: str, now: float, claimed: set[str]) -> Agent:
    pane = env.get("ZELLIJ_PANE_ID")
    root = process_root(env, root)
    rcwd = os.path.realpath(cwd) if cwd else None
    base: dict[str, Any] = dict(pid=pid, cwd=rcwd, session_name=env.get("ZELLIJ_SESSION_NAME") or None,
                                pane_id=int(pane) if pane and pane.isdigit() else None, start_time=start,
                                provider=SOURCE)
    registry = read_registry(pid, root)
    if registry and registry.get("startedAt") and start and registry["startedAt"] < start - 60:
        registry = None          # left over from an earlier process with this pid
    sid, _how = resolve_session(pid, argv, env, start, hooks, registry)
    rec = hooks.get(sid) if sid else None
    cwds = [c for c in dict.fromkeys([(rec or {}).get("cwd"), (registry or {}).get("cwd"), cwd, rcwd])
            if isinstance(c, str) and c]
    path = None
    if sid:
        path = find_transcript(sid, cwds, root, hint=(rec or {}).get("transcript_path"))
    else:
        for c in cwds:
            for _m, p in newest_transcripts(c, start, root):
                if p not in claimed:
                    path = p
                    break
            if path:
                break
    if path:
        claimed.add(path)
    tr = read_transcript(path) if path else None
    if sid is None and tr is not None:
        # the file name is the session id (what find_transcript looks up); records are the fallback
        sid = _safe_id(os.path.basename(path or "")[:-6]) or tr.get("session_id") or None

    status: AgentStatus = "unknown"
    since: int | None = None
    source: str | None = None
    if _hook_live(rec, start):
        status, since = hook_status(rec, now)        # type: ignore[arg-type]
        source = "hooks"
        if status in ("busy", "needs_input") and tr is not None and interrupted_after(tr, rec):
            # Esc sends no Stop hook: the transcript's interrupt marker, newer than the record, wins
            status = "idle"
            mt = _num(tr.get("mtime"))
            since = None if mt is None else max(0, int(now - mt))
    elif registry and registry.get("status") in ("busy", "idle"):
        status = registry["status"]                  # type: ignore[assignment]
        mt = _num((tr or {}).get("mtime")) or registry.get("updatedAt")
        since = None if mt is None else max(0, int(now - mt))
        if status == "idle" and since is not None and since >= DONE_AFTER_S:
            status = "done"
        source = "registry"
    elif tr is not None:
        status, since = transcript_status(tr, now)
        source = "transcript" if status != "unknown" else None
    t = tr or {}
    title = (t.get("custom_title") or t.get("ai_title") or t.get("summary") or (registry or {}).get("name")
             or t.get("first_prompt"))
    last = (rec or {}).get("last_message") if _hook_live(rec, start) or (rec or {}).get("status") == "ended" else None
    last = _trim(last) if isinstance(last, str) and last.strip() else t.get("last_message")
    model = t.get("model")
    return Agent(**base, status=status, status_since_s=since, title=title, session_id=sid,
                 last_message=last, context_tokens=t.get("context_tokens"),
                 context_limit=context_limit(model) if t.get("context_tokens") else None,
                 status_source=source, **status_quality(source))


def status_quality(source: str | None) -> dict[str, Any]:
    """status_quality / status_note for a status_source: hooks exact; registry / transcript
    approximate; nothing unavailable."""
    if source == "hooks":
        return {"status_quality": "exact"}
    if source in ("registry", "transcript"):
        return {"status_quality": "approximate", "status_note": APPROXIMATE_NOTE}
    return {"status_quality": "unavailable"}


def _fallback(pid: int, env: dict[str, str], cwd: str | None, start: float | None) -> Agent:
    pane = env.get("ZELLIJ_PANE_ID")
    return Agent(pid=pid, cwd=os.path.realpath(cwd) if cwd else None,
                 session_name=env.get("ZELLIJ_SESSION_NAME") or None,
                 pane_id=int(pane) if pane and pane.isdigit() else None, start_time=start, provider=SOURCE)


def fetch(timeout: float = 1.0, *, ps: list[tuple[int, int, str]] | None = None,
          pids: Iterable[int] | None = None, now: float | None = None
          ) -> tuple[tuple[Agent, ...], list[SourceWarning]]:
    """All running interactive Claude Code agents.

    `ps`: pre-fetched `proc.list_procs()` rows (else `ps` runs here). `pids` (quick refresh):
    skip discovery and look only at these processes; ones that exited or are not claude are
    dropped silently (callers may pass every known agent pid)."""
    warnings: list[SourceWarning] = []
    try:
        infos: dict[int, tuple[tuple[str, ...], dict[str, str]]] = {}
        if pids is not None:
            for pid in dict.fromkeys(int(p) for p in pids):
                ae = procmod.proc_args_env(pid)
                if ae:
                    infos[pid] = ae
            cand = list(infos)
        else:
            rows = ps if ps is not None else procmod.list_procs(timeout=timeout)
            if not rows:
                return (), [SourceWarning(SOURCE, "ps failed")]
            cand = find_claude(rows)
            cmd_by_pid = {p: c for p, _pp, c in rows}
            for pid in cand:
                ae = procmod.proc_args_env(pid)
                infos[pid] = ae if ae else (tuple(cmd_by_pid.get(pid, "").split()), {})
        cand = [p for p in cand if is_claude_argv(infos[p][0])]
        if not cand:
            return (), warnings
        cwds = dict(procmod.proc_cwd(cand, timeout=timeout))
        starts = procmod.start_times(cand)
        hooks = read_statuses()
        root = claude_dir()
        t_now = time.time() if now is None else now
    except Exception as e:  # noqa: BLE001 - never raise
        return (), [SourceWarning(SOURCE, f"failed: {type(e).__name__}: {e}"[:200])]
    agents: list[Agent] = []
    claimed: set[str] = set()
    # processes with a known session first, so transcript guessing never steals their file;
    # among the rest, the newest process gets the newest transcript
    order = sorted(cand, key=lambda p: (claude_session_arg(infos[p][0]) is None, -(starts.get(p) or 0)))
    for pid in order:
        argv, env = infos[pid]
        try:
            agents.append(_agent(pid, argv, env, cwds.get(pid), starts.get(pid), hooks, root, t_now, claimed))
        except Exception as e:  # noqa: BLE001 - one bad transcript must not hide the agent
            agents.append(_fallback(pid, env, cwds.get(pid), starts.get(pid)))
            warnings.append(SourceWarning(SOURCE, f"pid {pid}: {type(e).__name__}: {e}"[:200]))
    agents.sort(key=lambda a: a.pid)
    return tuple(agents), warnings


# --------------------------------------------------------------------------- hook status watcher

def status_stamp() -> int | None:
    """Change stamp of the hook status dir (its mtime_ns: every atomic record replace renames
    into it); None if it does not exist."""
    try:
        return os.stat(status_dir()).st_mtime_ns
    except OSError:
        return None


class StatusWatcher:
    """Polls the hook status dir (one stat per `period`) while `update()` was given at least one
    claude agent, and calls `on_change()` when it changed. The thread starts lazily and is a
    daemon; `close()` is prompt."""

    def __init__(self, on_change, *, period: float = 0.5, stamp=status_stamp) -> None:
        self._on_change = on_change
        self.period = period
        self._stamp = stamp
        self._lock = threading.Lock()
        self._active = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, agents: Iterable[Agent]) -> None:
        want = any(getattr(a, "provider", None) == SOURCE for a in agents)
        with self._lock:
            if self._stop.is_set():
                return
            if want:
                self._active.set()
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="agentree-claude-status", daemon=True)
                    self._thread.start()
            else:
                self._active.clear()

    @property
    def active(self) -> bool:
        return self._active.is_set() and not self._stop.is_set()

    def healthy(self) -> dict[int, bool]:
        """No per-agent event feed (the watcher only signals "something changed")."""
        return {}

    def _run(self) -> None:
        last = self._stamp()
        while not self._stop.is_set():
            if not self._active.wait(self.period):
                continue
            if self._stop.wait(self.period):
                return
            try:
                cur = self._stamp()
            except Exception:  # noqa: BLE001
                cur = None
            if cur != last:
                last = cur
                try:
                    self._on_change()
                except Exception:  # noqa: BLE001 - a broken callback must not kill the watcher
                    pass

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            self._active.set()       # wake the thread so it sees _stop
            t = self._thread
        if t is not None:
            t.join(0.2)


# --------------------------------------------------------------------------- housekeeping

PRUNE_EVERY_S = 3600.0
_prune_lock = threading.Lock()
_last_prune: float | None = None


def maybe_prune(now: float | None = None) -> bool:
    """prune() (old hook status records) at most once per PRUNE_EVERY_S per process; True if it
    ran. Never raises. Run by fetch_full (never by doctor)."""
    global _last_prune
    t = time.monotonic() if now is None else now
    with _prune_lock:
        if _last_prune is not None and t - _last_prune < PRUNE_EVERY_S:
            return False
        _last_prune = t
    try:
        prune()
    except Exception:  # noqa: BLE001 - housekeeping only
        pass
    return True


# --------------------------------------------------------------------------- doctor

def _hook_argv0() -> str:
    """Executable the Claude hooks run (first token of hook_command() that is not a VAR=value
    assignment)."""
    for tok in shlex.split(hook_command()):
        if "=" in tok and not tok.startswith(("/", ".")) and tok.split("=", 1)[0].isidentifier():
            continue
        return tok
    return ""


def _running_agents(timeout: float) -> list:
    """Running claude agents (fetch without housekeeping; status_source tells hook coverage)."""
    agents, _w = fetch(timeout=timeout)
    return list(agents)


@dataclass
class DoctorProbe:
    """Claude-local doctor access (the generic Probe has no claude fields). `resolve(cfg)` -> argv
    or None (None field: the provider's resolve_command); `claude_dir()`, `settings_path()`,
    `hook_argv0()`, `agents(timeout)` -> running claude agents."""
    resolve: Callable[[Any], tuple[str, ...] | None] | None = None
    claude_dir: Callable[[], str] = claude_dir
    settings_path: Callable[[], str] = settings_path
    hook_argv0: Callable[[], str] = _hook_argv0
    agents: Callable[[float], list] = _running_agents


def check_claude(cfg: Any, probe: Any, local: DoctorProbe, spec: ProviderSpec,
                 resolve: Callable[[Any], tuple[str, ...] | None]) -> list[Check]:
    """Claude Code is optional: nothing here is a FAIL except an unwritable hooks dir."""
    from agentree import diagnostics as dg
    from agentree.providers.command import configured_command

    S = spec.id
    h = probe.home
    raw = configured_command(spec, cfg)
    shown_cmd = raw if raw is not None else spec.default_command
    out: list[Check] = []
    try:
        argv = (local.resolve or resolve)(cfg)
    except Exception as e:  # noqa: BLE001 - reported
        argv = None
        out.append(dg.Check(S, "warn", f"{spec.command_key} {shown_cmd}: lookup failed: {e}"))
    if argv:
        out.append(dg.Check(S, "ok", f"{spec.command_key} {shown_cmd} -> {dg.tilde(argv[0], h)}"))
    elif not out:
        out.append(dg.Check(S, "warn", f"{spec.command_key} {shown_cmd}: not found (Claude Code agents disabled; "
                                       "optional)"))
    cdir = local.claude_dir()
    if probe.exists(cdir):
        out.append(dg.Check(S, "ok", f"claude dir {dg.tilde(cdir, h)}"))
    else:
        out.append(dg.Check(S, "ok", f"claude dir {dg.tilde(cdir, h)} not found (Claude Code not used yet)"))
    sdir = os.path.dirname(local.settings_path())
    ok, how = dg.writable_or_creatable(sdir, probe)
    out.append(dg.Check(S, "ok" if ok else "fail", f"hooks settings dir {dg.tilde(sdir, h)} {how}"))
    try:
        exe = local.hook_argv0()
    except Exception as e:  # noqa: BLE001
        exe = ""
        out.append(dg.Check(S, "warn", f"hook command unavailable: {e}"))
    if exe:
        found = exe if os.path.isabs(exe) else (probe.which(exe) or "")
        if found and probe.is_exec(found):
            out.append(dg.Check(S, "ok", f"hook command runs {dg.tilde(found, h)}"))
        else:
            out.append(dg.Check(S, "warn", f"hook command {dg.tilde(exe, h)} is not executable "
                                           "(claude status falls back to the transcript)"))
    try:
        agents = local.agents(dg.CMD_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - reported
        return out + [dg.Check(S, "warn", f"cannot list claude processes: {e}")]
    if not agents:
        out.append(dg.Check(S, "ok", "no running claude agents"))
        return out
    hooked = sum(1 for a in agents if getattr(a, "status_source", None) == "hooks")
    text = f"{len(agents)} running claude agent(s), {hooked} with hook status"
    if hooked < len(agents):
        text += " (the others: approximate status from Claude's registry / transcript)"
    out.append(dg.Check(S, "ok", text))
    return out


# --------------------------------------------------------------------------- provider

WRITE_HOOK_SETTINGS = "write_hook_settings"


def _split_cmd(cmd: str | Sequence[str]) -> list[str]:
    """A command (string or argv) as argv, argv0 `~`-expanded."""
    if isinstance(cmd, str):
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = [cmd]
        parts = parts or [cmd]
    else:
        parts = list(cmd)
    return [os.path.expanduser(parts[0]), *parts[1:]] if parts else [""]


def _in_panes(env: Mapping[str, str], session: str, pane_ids: frozenset[int] | set[int]) -> bool:
    pane = env.get("ZELLIJ_PANE_ID", "")
    return env.get("ZELLIJ_SESSION_NAME") == session and pane.isdigit() and int(pane) in pane_ids


def settings_step(path: str) -> SetupStep:
    """The setup step that (re)writes the hooks settings file `path` before claude starts."""
    return SetupStep(WRITE_HOOK_SETTINGS, f"write claude hook settings {path}", (("settings_path", path),))


class ClaudeProvider:
    """The Claude Code provider (see agentree.providers.base.Provider).

    `watcher_factory(on_change)` builds the hook status watcher (default StatusWatcher);
    `doctor_probe` the claude-local doctor access."""

    quick_failure: Literal["keep_snapshot", "keep_provider"] = "keep_provider"
    FULL_EVERY = 10.0
    QUICK_EVERY = 3.0

    def __init__(self, spec: ProviderSpec, *, watcher_factory: Callable[..., Any] | None = None,
                 doctor_probe: DoctorProbe | None = None) -> None:
        self.spec = spec
        self.id = spec.id
        self._watcher_factory = watcher_factory
        self.doctor_probe = doctor_probe

    # -- processes
    def matches_argv(self, argv: Sequence[str]) -> bool:
        return is_claude_argv(tuple(argv or ()))

    def find_pids(self, rows: Sequence[Any]) -> tuple[int, ...]:
        return tuple(find_claude(rows))

    def session_id_from_argv(self, argv: Sequence[str]) -> str | None:
        return claude_session_arg(tuple(argv or ()))

    # -- launch
    def resolve_command(self, cfg: Any) -> tuple[str, ...] | None:
        """claude_cmd (default "claude"): PATH + the usual extra dirs, then ~/.local/bin and
        ~/.claude/local."""
        from agentree.providers.command import resolve_for

        return resolve_for(self.spec, cfg)

    def build_launch(self, cfg: Any, request: LaunchRequest) -> Launch:
        """`<claude_cmd> --settings <hooks> --session-id <id> [--] [<prompt>]` plus the setup step
        that writes the hooks settings. The session id is request.session_id (generated at plan
        time), else a new uuid. spawn without a prompt and without the "agent" layout: nothing
        is started (a warning; the named layout runs as is)."""
        if request.mode == "spawn" and not request.prompt and request.layout != "agent":
            return Launch(argv=None, warnings=("no prompt and no agent layout: claude is not started "
                                               "(choose layout agent)",))
        errors: tuple[str, ...] = ()
        cmd: Sequence[str] | None = self.resolve_command(cfg)
        if cmd is None:
            from agentree.providers import registry
            from agentree.providers.command import configured_command

            errors = (registry.missing_command_message(self.id, cfg),)
            if request.mode == "pane":
                return Launch(argv=None, errors=errors)
            cmd = _split_cmd(configured_command(self.spec, cfg) or self.spec.default_command)
        sid = request.session_id or new_session_id()
        settings = request.settings_path or settings_path()
        argv = (*cmd, *launch_args(sid, request.prompt or None, settings=settings))
        return Launch(argv=tuple(argv), session_id=sid, setup=(settings_step(settings),), errors=errors)

    def execute_setup(self, step: SetupStep, *, runner: Runner, state_dir: str) -> str:
        """write_hook_settings: (re)write the hooks settings file through the runner (skipped under
        dry-run); OSError propagates."""
        if step.operation != WRITE_HOOK_SETTINGS:
            raise ValueError(f"{self.id}: unknown setup step {step.operation!r}")
        path = dict(step.payload).get("settings_path")
        path = path if isinstance(path, str) and path else settings_path()
        runner.internal(f"write claude hook settings {path}", lambda: write_settings(path=path))
        return ""

    # -- status
    def fetch_full(self, ctx: FetchContext) -> FetchResult:
        maybe_prune()
        return fetch(timeout=ctx.timeout_s, now=ctx.now)

    def fetch_quick(self, agents: Sequence[Agent], ctx: FetchContext) -> FetchResult:
        return fetch(timeout=ctx.timeout_s, pids=[a.pid for a in agents], now=ctx.now)

    def fetch_budget_s(self, timeout_s: float, *, quick: bool) -> float:
        # full: ps, cwd (lsof fallback), then local file reads (status records, transcripts)
        return (2 if quick else 3) * timeout_s

    def start_watcher(self, poke: Callable[[], None]) -> Watcher | None:
        factory = self._watcher_factory or StatusWatcher
        return factory(poke)

    def refresh_policy(self, agents: Sequence[Agent], watcher: Watcher | None) -> RefreshPolicy:
        """Full FULL_EVERY; quick QUICK_EVERY while some agent's status is not covered by hook
        events (no watcher, or registry / transcript status) or may change without one (busy /
        needs input: an Esc interrupt sends no Stop hook, only the transcript shows it)."""
        quick = None
        for a in agents:
            if watcher is None or a.status_source != "hooks" or a.status in ("busy", "needs_input"):
                quick = self.QUICK_EVERY
                break
        return RefreshPolicy(self.FULL_EVERY, quick)

    def find_launched_pids(self, session: str, pane_ids: frozenset[int], *, session_id: str | None,
                           timeout_s: float) -> tuple[int, ...]:
        """PIDs of claude agents in `pane_ids` of `session`; when one of them carries our
        `--session-id`, only those (a pane may also hold a nested / older claude)."""
        rows = procmod.list_procs(timeout=timeout_s)
        hits: list[tuple[int, tuple[str, ...]]] = []
        for pid in self.find_pids(rows):
            ae = procmod.proc_args_env(pid)
            if ae and _in_panes(ae[1], session, pane_ids):
                hits.append((pid, tuple(ae[0])))
        if session_id:
            mine = [pid for pid, argv in hits if claude_session_arg(argv) == session_id]
            if mine:
                return tuple(sorted(mine))
        return tuple(sorted(pid for pid, _a in hits))

    # -- doctor / cli / demo
    def doctor_jobs(self, cfg: Any, probe: Probe, *, local: DoctorProbe | None = None) -> tuple[DoctorJob, ...]:
        loc = local or self.doctor_probe or DoctorProbe()
        return (DoctorJob(self.id, lambda: check_claude(cfg, probe, loc, self.spec, self.resolve_command)),)

    def run_internal(self, command: str, argv: Sequence[str]) -> int:
        if command == "claude-hook":
            return hook_main()
        raise KeyError(command)

    def demo_agents(self, ctx: DemoContext) -> tuple[DemoAgent, ...]:
        simd = ctx.paths["simd"]
        sid = "0b5e2c4e-7d1a-4f3e-9c2b-5a6d7e8f9a01"
        settings = f"{ctx.paths['home']}/.local/state/agentree/claude/settings.json"
        pane = ZPane(pane_id=3, is_plugin=False, tab_id=21, title="claude", command="claude",
                     argv=("claude", "--settings", settings, "--session-id", sid), cwd=simd)
        agent = Agent(pid=4302, cwd=simd, session_name="engine", pane_id=3, status="busy", status_since_s=45,
                      title="Vectorise the probe loop",
                      last_message="Rewrote probe() with NEON intrinsics; running the benchmarks.",
                      context_tokens=61_000, context_limit=200_000, provider=SOURCE, session_id=sid,
                      status_source="hooks", **status_quality("hooks"))
        return (DemoAgent(agent, "engine", 21, pane),)


def create_provider(spec: ProviderSpec) -> ClaudeProvider:
    return ClaudeProvider(spec)
