"""Collect every source in parallel and link them into a Snapshot (also written to the cache).

Wall time is bounded: zellij, git and every agent provider start together on daemon threads; kitty starts
as soon as zellij's session names are known (it needs them to match windows) and overlaps the
git status pass, which dominates (`cfg.git_budget_s`). A source that raises or overruns its
deadline contributes a SourceWarning and no data; a hung source thread is abandoned (daemon),
never joined, so `gather()` and process exit are never blocked by it. While an abandoned
invocation of a source is still alive, that source is not launched again (no thread pile-up):
the next gather reports "<source>: previous refresh still running" for it instead.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Any, Callable, Sequence

from agentree.config import Config
from agentree.types import Snapshot, SourceWarning

SLACK_S = 0.5            # grace on top of a source's own timeouts before it is abandoned

TIMINGS: dict[str, float] = {}   # per-source wall ms of the last gather() ("total" included)

_inflight_lock = threading.Lock()
_inflight: dict[str, "_Job"] = {}   # source name -> its most recent job (alive or finished)


class _Job:
    """Run fn on a daemon thread; result() waits until a monotonic deadline."""

    def __init__(self, name: str, fn: Callable[[], tuple[Any, list[SourceWarning]]]) -> None:
        self.name = name
        self._fn = fn
        self._done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None
        self.t0 = time.monotonic()
        self.elapsed: float | None = None
        threading.Thread(target=self._run, name=f"agentree-{name}", daemon=True).start()

    def _run(self) -> None:
        try:
            self.value = self._fn()
        except BaseException as e:  # noqa: BLE001 - reported as a warning
            self.error = e
        finally:
            self.elapsed = time.monotonic() - self.t0
            self._done.set()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def peek(self, deadline: float) -> tuple:
        """Records if the job finished OK by `deadline`, else (); never warns (see result())."""
        if not self._done.wait(max(0.0, deadline - time.monotonic())) or self.error is not None:
            return ()
        try:
            records, _ = self.value
        except (TypeError, ValueError):
            return ()
        return tuple(records or ())

    def result(self, deadline: float, warnings: list[SourceWarning]) -> tuple:
        """Return the records tuple (empty on failure), appending the source's warnings."""
        if not self._done.wait(max(0.0, deadline - time.monotonic())):
            warnings.append(SourceWarning(self.name, f"timed out after {deadline - self.t0:.1f}s"))
            return ()
        if self.error is not None:
            msg = f"{type(self.error).__name__}: {self.error}".strip()
            warnings.append(SourceWarning(self.name, f"failed: {msg[:200]}"))
            return ()
        try:
            records, warns = self.value
        except (TypeError, ValueError):
            warnings.append(SourceWarning(self.name, "failed: bad fetch() result"))
            return ()
        warnings.extend(warns or ())
        return tuple(records or ())


class _Busy:
    """Stand-in for a source whose previous invocation is still running."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.t0 = time.monotonic()
        self.elapsed = None

    def peek(self, deadline: float) -> tuple:
        return ()

    def result(self, deadline: float, warnings: list[SourceWarning]) -> tuple:
        warnings.append(SourceWarning(self.name, "previous refresh still running"))
        return ()


def _launch(name: str, fn: Callable[[], tuple[Any, list[SourceWarning]]]) -> "_Job | _Busy":
    """Start `fn` unless the previous invocation of this source is still alive."""
    with _inflight_lock:
        prev = _inflight.get(name)
        if prev is not None and not prev.done:
            return _Busy(name)
        job = _Job(name, fn)
        _inflight[name] = job
        return job


def inflight() -> list[str]:
    """Names of sources whose last invocation is still running."""
    with _inflight_lock:
        return sorted(n for n, j in _inflight.items() if not j.done)


@dataclasses.dataclass(frozen=True)
class RawInventory:
    """Non-agent inputs of one full gather, as observed (relinked by Gatherer.agents)."""
    sessions: tuple
    repos: tuple
    hosts: tuple
    links: tuple
    warnings_pre: tuple          # warnings before the providers' (config, zellij, git)
    warnings_post: tuple         # after (kitty, state)
    observed_at: float           # unix time the relative ages refer to (= the full snapshot's taken_at)
    generation: int = 0          # set by the Gatherer that published it


TITLES_WAIT_S = 0.1      # quick refresh: wait this long for fresh pane titles, else use the last known


def pane_titles(sessions) -> dict[tuple[str, int], str]:
    """{(session name, terminal pane id): pane title} for live sessions (agent session disambiguation)."""
    out: dict[tuple[str, int], str] = {}
    for s in sessions:
        if s.state == "exited":
            continue
        for tab in s.tabs:
            for p in tab.panes:
                if not p.is_plugin and p.title:
                    out[(s.name, p.pane_id)] = p.title
    return out


def _providers(providers: Sequence[Any] | None) -> tuple:
    """The agent providers to use: `providers` (tests inject them), else the registry's (in
    registry order: PID precedence when agents are merged)."""
    if providers is not None:
        return tuple(providers)
    from agentree.providers import registry

    return registry.all_providers()


def gather(cfg: Config, *, now: float | None = None,
           sink: Callable[[RawInventory], None] | None = None,
           providers: Sequence[Any] | None = None) -> Snapshot:
    """Fetch all sources, link, save the cache, return the snapshot (stale=False). `sink`
    receives the non-agent inputs (live status relinks them, see Gatherer); without one they
    go to the module's default Gatherer, which only the module-level gather_agents() uses.
    `providers`: the agent providers (default: the registry's)."""
    if sink is None:
        sink = _DEFAULT.publish
    from agentree import state
    from agentree.model.link import link
    from agentree.providers.base import FetchContext
    from agentree.sources import git, kitty, zellij

    provs = _providers(providers)
    t_start = time.monotonic()
    t = cfg.source_timeout_s
    # zellij: list-sessions, per-session inventory, one inventory retry (three timed phases).
    zellij_deadline = t_start + 3 * t + SLACK_S
    git_deadline = t_start + cfg.git_budget_s + SLACK_S

    zdirs = list(cfg.zellij_socket_dirs) or None
    globs = list(cfg.expanded_repo_roots())
    j_zellij = _launch("zellij", lambda: zellij.fetch(zdirs, timeout=t))
    j_git = _launch("git", lambda: git.fetch(globs, timeout=t, budget_s=cfg.git_budget_s))
    # providers overlap zellij: one may wait for zellij's pane titles (after its process
    # discovery), at most until t_start + 2t so a final phase (<= t) still fits its deadline.
    ctx = FetchContext(timeout_s=t, now=None,
                       pane_titles=lambda: pane_titles(j_zellij.peek(t_start + 2 * t)))
    jobs = [(p, _launch(p.id, lambda p=p: p.fetch_full(ctx))) for p in provs]

    warnings: list[SourceWarning] = [SourceWarning("config", w) for w in cfg.warnings]
    sessions = j_zellij.result(zellij_deadline, warnings)
    names = [s.name for s in sessions if s.state != "exited"]
    hosts: tuple = ()
    j_kitty = None
    if names:
        j_kitty = _launch("kitty", lambda: kitty.fetch(names, timeout=t, socket_glob=cfg.kitty_socket_glob))
    repos = j_git.result(git_deadline, warnings)
    pre = len(warnings)
    agents = merge_agents(*(j.result(t_start + p.fetch_budget_s(t, quick=False) + SLACK_S, warnings)
                            for p, j in jobs))
    n_agents = len(warnings) - pre           # provider warnings (replaced by quick refreshes)
    if j_kitty is not None:
        hosts = j_kitty.result(max(git_deadline, j_kitty.t0 + t + SLACK_S), warnings)

    links: list = []
    try:
        links = state.load_links()
    except Exception as e:  # noqa: BLE001
        warnings.append(SourceWarning("state", f"links.json unreadable: {e}"))

    taken = time.time() if now is None else now
    snap = link(sessions, repos, agents, hosts, links, warnings, now=taken)
    if sink is not None:
        sink(RawInventory(tuple(sessions), tuple(repos), tuple(hosts), tuple(links),
                          tuple(warnings[:pre]), tuple(warnings[pre + n_agents:]), snap.taken_at))
    try:
        state.save_cache(snap)
    except OSError as e:
        snap = dataclasses.replace(snap, warnings=snap.warnings + (SourceWarning("state", f"cache not saved: {e}"),))

    TIMINGS.clear()
    for j in (j_zellij, j_git, *(j for _p, j in jobs), j_kitty):
        if j is not None and j.elapsed is not None:
            TIMINGS[j.name] = j.elapsed * 1000
    TIMINGS["total"] = (time.monotonic() - t_start) * 1000
    return snap


# --------------------------------------------------------------------------- live status

def merge_agents(*groups) -> tuple:
    """Concatenate agent tuples (in provider registry order); the first record of a pid wins."""
    seen: set[int] = set()
    out = []
    for g in groups:
        for a in g or ():
            if a.pid not in seen:
                seen.add(a.pid)
                out.append(a)
    return tuple(out)


def snapshot_agents(snap: Snapshot) -> list:
    """Every agent in a snapshot (placed, loose, orphan)."""
    out = [a for n in snap.sessions for w in n.worktrees for a in w.agents]
    out += [a for n in snap.sessions for a in n.loose_agents]
    out += list(snap.orphans.agents)
    return out


def rebase_ages(raw: RawInventory, elapsed: int) -> tuple[tuple, tuple]:
    """(sessions, repos) with every relative age advanced by `elapsed` whole seconds, so they
    stay consistent with a snapshot taken `elapsed` s after the inventory was observed."""
    if elapsed <= 0:
        return raw.sessions, raw.repos
    sessions = tuple(dataclasses.replace(s, created_age_s=s.created_age_s + elapsed)
                     if s.created_age_s is not None else s for s in raw.sessions)
    repos = tuple(dataclasses.replace(r, worktrees=tuple(
        dataclasses.replace(w, last_commit_age_s=w.last_commit_age_s + elapsed)
        if w.last_commit_age_s is not None else w for w in r.worktrees)) for r in raw.repos)
    return sessions, repos


@dataclasses.dataclass(frozen=True)
class _Titles:
    """A pane-title fetch result, tagged with the inventory it was requested for."""
    generation: int
    targets: frozenset           # {(session name, socket dir)} that were listed
    titles: dict
    failed: tuple


class Gatherer:
    """Full + agents-only gathers sharing one inventory (owned by a LiveStatus; no globals).

    full()          gather(); publishes its RawInventory as a new generation and drops title
                    results that belong to older generations
    agents(prev)    agents-only refresh, ~10-110 ms: re-query the agent processes already in
                    `prev` (each provider gets only its own agents and is not started without
                    any; no `ps`: new agents appear with the next full gather) and the pane
                    titles of the zellij sessions hosting them, then relink with the current
                    inventory, its relative ages advanced to the new snapshot time (taken_at is
                    observed_at + whole elapsed seconds, so taken_at - created_age_s, the
                    session's creation time used by restart detection, is unchanged). Without an
                    inventory this is a full gather. A provider fetch that fails, overruns or is
                    still busy follows the provider's `quick_failure`: "keep_snapshot" returns
                    `prev` unchanged, "keep_provider" keeps that provider's agents and warnings
                    of `prev`. A provider without agents keeps its warnings of `prev`. The cache
                    file is not rewritten.
    `providers`: the agent providers (default: the registry's, looked up on each use).
    """

    def __init__(self, providers: Sequence[Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._raw: RawInventory | None = None
        self._generation = 0
        self._late: _Titles | None = None
        self._titles_job = f"zellij-titles@{id(self):x}"
        self.timings: dict[str, float] = {}
        self._providers = tuple(providers) if providers is not None else None

    @property
    def providers(self) -> tuple:
        # injected ones, else the registry's at call time (instances are cached there; the
        # module-level _DEFAULT must follow registry.override)
        return self._providers if self._providers is not None else _providers(None)

    # -- inventory
    @property
    def raw(self) -> RawInventory | None:
        return self._raw

    def publish(self, raw: RawInventory) -> None:
        with self._lock:
            self._generation += 1
            self._raw = dataclasses.replace(raw, generation=self._generation)
            self._late = None

    def _store_titles(self, res: _Titles) -> None:
        with self._lock:
            if self._raw is not None and res.generation == self._raw.generation:
                self._late = res

    def late_titles(self, generation: int) -> _Titles | None:
        with self._lock:
            late = self._late
        return late if late is not None and late.generation == generation else None

    # -- gathers
    def full(self, cfg: Config, *, now: float | None = None) -> Snapshot:
        return gather(cfg, now=now, sink=self.publish, providers=self.providers)

    def agents(self, cfg: Config, prev: Snapshot, *, now: float | None = None) -> Snapshot:
        from agentree.model.link import link
        from agentree.providers.base import FetchContext
        from agentree.sources import zellij

        raw = self._raw
        if raw is None:
            return self.full(cfg, now=now)
        t_start = time.monotonic()
        t = min(cfg.source_timeout_s, 1.0)
        prev_agents = snapshot_agents(prev)
        if not prev_agents:
            return prev
        live = {s.name: s.socket_dir for s in raw.sessions if s.state != "exited" and s.socket_dir}
        targets = frozenset((a.session_name, live[a.session_name]) for a in prev_agents
                            if a.session_name in live)
        known = pane_titles(raw.sessions)
        gen = raw.generation

        def fetch_titles():
            got, failed = zellij.fetch_pane_titles(sorted(targets), timeout=t)
            res = _Titles(gen, targets, got, tuple(failed))
            self._store_titles(res)          # kept for a later call if it finishes too late for us
            return (res,), []
        j_titles = _launch(self._titles_job, fetch_titles)

        def overlay(res: _Titles) -> dict:
            # only sessions this result listed, with the same identity (name + socket dir) as in
            # the inventory; failed ones keep the inventory's titles
            ok = {n for n, d in res.targets if live.get(n) == d and n not in res.failed}
            out = {k: v for k, v in known.items() if k[0] not in ok}
            out.update((k, v) for k, v in res.titles.items() if k[0] in ok)
            return out

        def titles() -> dict:
            fresh = j_titles.peek(t_start + min(t, TITLES_WAIT_S))
            if fresh:
                return overlay(fresh[0])
            late = self.late_titles(gen)
            return overlay(late) if late is not None else known

        ctx = FetchContext(timeout_s=t, now=None, pane_titles=titles)
        # each provider re-queries only its own agents; a provider with none is not started
        jobs = []
        for p in self.providers:
            mine = [a for a in prev_agents if a.provider == p.id]
            j = _launch(p.id, lambda p=p, mine=mine: p.fetch_quick(mine, ctx)) if mine else None
            jobs.append((p, mine, j))
        groups: list[tuple] = []
        ow: list[SourceWarning] = []
        for p, mine, j in jobs:
            if j is None:            # no agents of this provider: keep its warnings of the last full gather
                ow += [w for w in prev.warnings if w.source == p.id]
                continue
            pw: list[SourceWarning] = []
            got = j.result(t_start + p.fetch_budget_s(t, quick=True) + SLACK_S, pw)
            if not isinstance(j, _Job) or not j.done or j.error is not None:
                # failed / overran / still busy
                if p.quick_failure == "keep_snapshot":
                    return prev
                got = tuple(mine)
                pw = [w for w in prev.warnings if w.source == p.id]
            groups.append(got)
            ow += pw
        agents = merge_agents(*groups)
        wall = time.time() if now is None else now
        elapsed = max(0, int(wall - raw.observed_at))
        sessions, repos = rebase_ages(raw, elapsed)
        snap = link(sessions, repos, agents, raw.hosts, raw.links,
                    list(raw.warnings_pre) + ow + list(raw.warnings_post), now=raw.observed_at + elapsed)
        self.timings = {"total": (time.monotonic() - t_start) * 1000}
        for name, j in (("titles", j_titles), *((p.id, j) for p, _m, j in jobs)):
            if j is not None and j.elapsed is not None:
                self.timings[name] = j.elapsed * 1000
        return snap


# Backs the module-level gather() / gather_agents() pair (CLI, tests, scripts). LiveStatus and
# any other long-lived user own their Gatherer, so they never share inventory or title results
# with this one or with each other.
_DEFAULT = Gatherer()


def gather_agents(cfg: Config, prev: Snapshot, *, now: float | None = None) -> Snapshot:
    """Agents-only refresh against the inventory of the last module-level gather() (see
    Gatherer.agents; without one this is a full gather)."""
    return _DEFAULT.agents(cfg, prev, now=now)


class LiveStatus:
    """Glue between the TUI (`ui.app.run_app(live=...)`), the providers' status feeds and the two
    refresh kinds. Owns its Gatherer (inventory generations, title results).

    refresh()   full gather; then every provider's watcher follows the snapshot's agents of
                that provider
    quick(prev) agents-only refresh (Gatherer.agents); same watcher update
    start(poke) after the first frame: each provider starts its watcher (lazily, so the first
                frame is not slowed); every change a watcher reports -> poke() -> a quick refresh
    interval()  full refresh period: POLL_EVERY before start() and without agents; else the
                shortest `full_s` of the providers' refresh policies over their non-empty agent
                groups (e.g. a provider polls while its event feed is down)
    quick_interval()  the shortest non-null `quick_s` of those policies, else None. The app
                runs a quick refresh that often
    close()     stop all watchers (prompt, nothing joins for long)
    `providers`: the agent providers (default: the registry's), resolved on first use.
    """

    POLL_EVERY = 2.0

    def __init__(self, cfg: Config, *, full: Callable[[Config], Snapshot] | None = None,
                 quick: Callable[[Config, Snapshot], Snapshot] | None = None,
                 providers: Sequence[Any] | None = None,
                 gatherer: Gatherer | None = None) -> None:
        self.cfg = cfg
        self.gatherer = gatherer or Gatherer(providers)
        self._providers = tuple(providers) if providers is not None else None
        self._full = full            # None: this instance's Gatherer
        self._quick = quick
        self._lock = threading.Lock()
        self.watchers: dict[str, Any] = {}     # provider id -> its watcher (None: it has none)
        self._started = False
        self._agents: list | None = None
        self._closed = False

    @property
    def providers(self) -> tuple:
        if self._providers is None:
            self._providers = self.gatherer.providers
        return self._providers

    @staticmethod
    def _own(p: Any, agents: Sequence[Any] | None) -> list:
        return [a for a in agents or () if a.provider == p.id]

    def start(self, poke: Callable[[], None]) -> None:
        made: dict[str, Any] = {}
        for p in self.providers:
            try:
                made[p.id] = p.start_watcher(poke)
            except Exception:  # noqa: BLE001 - that provider then relies on polling
                made[p.id] = None
        with self._lock:
            if self._closed:
                closing = [w for w in made.values() if w is not None]
            else:
                closing = []
                self.watchers, self._started = made, True
            agents = self._agents
        for w in closing:
            _close_quietly(w)
        if closing or self._closed:
            return
        if agents is not None:
            self._update(made, agents)

    def _update(self, watchers: dict[str, Any], agents: Sequence[Any]) -> None:
        for p in self.providers:
            w = watchers.get(p.id)
            if w is not None:
                w.update(self._own(p, agents))

    def _watch(self, snap: Snapshot) -> None:
        agents = snapshot_agents(snap)
        with self._lock:
            self._agents = agents
            watchers = {} if self._closed else dict(self.watchers)
        self._update(watchers, agents)

    def refresh(self) -> Snapshot:
        snap = self._full(self.cfg) if self._full else self.gatherer.full(self.cfg)
        self._watch(snap)
        return snap

    def quick(self, prev: Snapshot) -> Snapshot:
        snap = self._quick(self.cfg, prev) if self._quick else self.gatherer.agents(self.cfg, prev)
        self._watch(snap)
        return snap

    def _policies(self) -> tuple[bool, list]:
        with self._lock:
            started = self._started and not self._closed
            agents, watchers = self._agents, dict(self.watchers)
        out = []
        for p in self.providers:
            mine = self._own(p, agents)
            if mine:
                out.append(p.refresh_policy(mine, watchers.get(p.id)))
        return started, out

    def interval(self) -> float:
        started, policies = self._policies()
        if not started or not policies:
            return self.POLL_EVERY
        return min(pol.full_s for pol in policies)

    def quick_interval(self) -> float | None:
        _started, policies = self._policies()
        quick = [pol.quick_s for pol in policies if pol.quick_s is not None]
        return min(quick) if quick else None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            ws, self.watchers = list(self.watchers.values()), {}
        for w in ws:
            if w is not None:
                _close_quietly(w)


def _close_quietly(w: Any) -> None:
    try:
        w.close()
    except Exception:  # noqa: BLE001 - closing must never fail the exit path
        pass
