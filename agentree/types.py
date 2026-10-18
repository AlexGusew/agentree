"""Shared data contract for agentree.

Every lane (sources, model, ui, actions) codes against these types. Change them only
with the orchestrator's agreement. All records are immutable; collections are tuples.

Layering:
  sources/*  -> raw records (ZSession, ZTab, ZPane, Repo, Worktree, Agent, KittyHost)
  model/link -> Snapshot (linked tree: session > worktree > {agents, panes})
  model/filters + model/tree -> View (flat list of Row for rendering)
  model/detail -> Detail for the selected row
  ui/*       -> renders View/Detail, never touches sources directly
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Union

# --------------------------------------------------------------------------- raw: zellij

SessionState = Literal["attached", "detached", "exited"]


@dataclass(frozen=True, slots=True)
class ZPane:
    pane_id: int                  # zellij numeric id within its namespace
    is_plugin: bool               # plugin ids live in a separate namespace
    tab_id: int                   # stable tab id
    title: str
    command: str | None           # basename of argv0 of the running terminal command, e.g. "nvim"
    argv: tuple[str, ...]         # full terminal command argv if known, else ()
    cwd: str | None               # absolute, realpath-normalised when possible
    focused: bool = False
    floating: bool = False
    exited: bool = False
    selectable: bool = True

    @property
    def zid(self) -> str:
        """Zellij CLI id: 'terminal_N' or 'plugin_N'."""
        return f"{'plugin' if self.is_plugin else 'terminal'}_{self.pane_id}"


@dataclass(frozen=True, slots=True)
class ZTab:
    tab_id: int                   # stable id (go-to-tab-by-id)
    position: int                 # 0-based; UI shows position+1
    name: str
    active: bool
    panes: tuple[ZPane, ...] = ()


@dataclass(frozen=True, slots=True)
class ZSession:
    name: str
    state: SessionState
    created_age_s: int | None     # seconds since creation, from list-sessions
    socket_dir: str | None        # ZELLIJ_SOCKET_DIR that hosts the live server; None if exited
    clients: int = 0              # connected clients (0 => detached)
    is_current: bool = False      # the session agentree itself runs in
    tabs: tuple[ZTab, ...] = ()   # empty for exited sessions (never fabricate tabs)
    inventory_complete: bool = True  # False if tabs/panes could not be listed


# --------------------------------------------------------------------------- raw: git

@dataclass(frozen=True, slots=True)
class Worktree:
    path: str                     # absolute realpath
    repo_root: str                # path of the main (root) worktree of this repo
    branch: str | None            # short name, None when detached
    head: str | None              # commit sha
    is_root: bool                 # main checkout
    detached: bool = False
    prunable: bool = False
    locked: bool = False
    dirty: int | None = None      # number of changed paths; None if status unavailable
    ahead: int | None = None      # vs base_ref
    behind: int | None = None
    base_ref: str | None = None   # upstream or default branch used for ahead/behind, e.g. "origin/main"
    last_commit: str | None = None  # subject
    last_commit_age_s: int | None = None

    @property
    def label(self) -> str:
        return self.branch or (f"({self.head[:7]})" if self.head else self.path.rsplit("/", 1)[-1])


@dataclass(frozen=True, slots=True)
class Repo:
    root: str
    name: str                     # basename of root
    default_ref: str | None       # e.g. "origin/main" from origin/HEAD
    worktrees: tuple[Worktree, ...] = ()


# --------------------------------------------------------------------------- raw: agents

AgentStatus = Literal["busy", "idle", "needs_input", "done", "unknown"]
StatusQuality = Literal["exact", "approximate", "unavailable"]

# Plain JSON-safe scalar for provider metadata (Agent.meta, setup-step payloads).
type ExtraValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class Agent:
    """A running agent CLI process. Provider-neutral: `provider` names the registry entry
    (agentree.providers.registry) that found it; provider-only facts live in `meta` (plain scalars,
    never secrets / env / transcript bodies). `meta` is copied into a private dict on
    construction and must be treated as immutable; it takes part in == but not in hash()."""
    pid: int
    cwd: str | None
    session_name: str | None      # ZELLIJ_SESSION_NAME from the process env
    pane_id: int | None           # ZELLIJ_PANE_ID (terminal pane id); None => "no pane"
    status: AgentStatus = "unknown"
    status_since_s: int | None = None   # seconds in current status (busy 2m / done 5h)
    title: str | None = None            # title of the agent's current / most recent session
    last_message: str | None = None     # last assistant text, trimmed
    context_tokens: int | None = None
    context_limit: int | None = None
    start_time: float | None = None     # process start (unix s); pid-reuse guard for actions
    provider: str = "opencode"          # registry provider id ("opencode", "claude", ...)
    session_id: str | None = None       # the provider's session id, if known
    status_source: str | None = None    # provider-defined origin of `status` (e.g. "hooks")
    status_quality: StatusQuality = "unavailable"
    status_note: str | None = None      # user-facing caveat, e.g. "Status is approximate"
    meta: Mapping[str, ExtraValue] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "meta", dict(self.meta or {}))


# --------------------------------------------------------------------------- raw: kitty

@dataclass(frozen=True, slots=True)
class KittyHost:
    """A kitty window whose foreground process is a zellij client for `session`."""
    socket: str                   # e.g. "unix:/tmp/kitty.sock-11854"
    os_window_id: int
    tab_id: int
    window_id: int
    session: str
    focused: bool = False


# --------------------------------------------------------------------------- linked snapshot

@dataclass(frozen=True, slots=True)
class WorktreeNode:
    worktree: Worktree
    tabs: tuple[ZTab, ...] = ()   # tabs (in this session) that have a pane inside the worktree; first = primary
    agents: tuple[Agent, ...] = ()
    panes: tuple[ZPane, ...] = ()  # non-agent, non-plugin panes inside the worktree
    via_link: bool = False         # placed by links.json rather than heuristics


@dataclass(frozen=True, slots=True)
class SessionNode:
    session: ZSession
    worktrees: tuple[WorktreeNode, ...] = ()
    loose_agents: tuple[Agent, ...] = ()   # agents in this session not inside any known worktree
    loose_panes: tuple[ZPane, ...] = ()    # panes in this session not inside any known worktree
    kitty: KittyHost | None = None


@dataclass(frozen=True, slots=True)
class SourceWarning:
    source: str                   # "zellij" | "git" | "kitty" | "proc" | a provider id
    message: str


@dataclass(frozen=True, slots=True)
class Orphans:
    worktrees: tuple[Worktree, ...] = ()   # rendered as the "Needs a home" group
    agents: tuple[Agent, ...] = ()         # agents with no zellij session/pane


@dataclass(frozen=True, slots=True)
class Snapshot:
    taken_at: float               # unix time
    sessions: tuple[SessionNode, ...] = ()
    orphans: Orphans = field(default_factory=Orphans)
    warnings: tuple[SourceWarning, ...] = ()
    stale: bool = False           # True when loaded from the on-disk cache


# --------------------------------------------------------------------------- ui state

WorktreeMode = Literal["all", "with_tab", "dirty", "hide"]
WORKTREE_MODES: tuple[WorktreeMode, ...] = ("all", "with_tab", "dirty", "hide")


@dataclass(frozen=True, slots=True)
class UiState:
    worktree_mode: WorktreeMode = "all"
    show_agents: bool = True
    show_panes: bool = False
    show_detached: bool = True
    text: str = ""
    folds: tuple[tuple[str, bool], ...] = ()   # explicit overrides: (row key, expanded)
    selected: str | None = None                # row key


# --------------------------------------------------------------------------- view (rows)

Role = Literal["text", "dim", "accent", "teal", "green", "red", "blue", "warn"]


@dataclass(frozen=True, slots=True)
class Seg:
    text: str
    role: Role = "text"
    bold: bool = False


RowKind = Literal["session", "worktree", "agent", "pane", "group", "warning", "footer"]
NodeRef = Union[SessionNode, WorktreeNode, Agent, ZPane, Orphans, SourceWarning, None]


@dataclass(frozen=True, slots=True)
class Row:
    key: str                      # stable: "s:<session>", "w:<session>:<path>", "a:<pid>",
                                  # "p:<session>:<zid>", "g:orphans", "ow:<path>", "warn:<i>", "footer"
    kind: RowKind
    depth: int                    # indent level; UI indents 2 cells per level
    caret: Literal["open", "closed"] | None
    icon: Seg | None
    label: tuple[Seg, ...]
    meta: tuple[Seg, ...]         # right-aligned
    ref: NodeRef = None
    session: str | None = None    # owning session name, if any
    parent: str | None = None     # parent row key
    selectable: bool = True       # footer/warnings are not


@dataclass(frozen=True, slots=True)
class View:
    rows: tuple[Row, ...]
    hidden_worktrees: int = 0
    hidden_sessions: int = 0      # detached sessions hidden by the D toggle
    hidden_by_text: int = 0       # sessions removed by the text filter


@dataclass(frozen=True, slots=True)
class DetailField:
    label: str
    value: tuple[Seg, ...]
    wrap: bool = False            # long text (last assistant message) wraps


@dataclass(frozen=True, slots=True)
class DetailAction:
    key: str                      # "enter", "o", "t", "g", "x", "r"
    label: str                    # "jump", "new agent here", ...
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Detail:
    kind: str                     # "SESSION" | "WORKTREE" | "AGENT" | "PANE" | "NEEDS A HOME" | ...
    title: tuple[Seg, ...]
    fields: tuple[DetailField, ...] = ()
    actions: tuple[DetailAction, ...] = ()
