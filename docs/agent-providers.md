# Agent providers

Every agent-specific behaviour lives in a provider (`agentree/providers/<id>.py`), described by one
`ProviderSpec` in `agentree/providers/registry.py` (order matters: when two providers claim a pid,
the earlier wins). Core code (gather, actions, doctor, config, UI) only talks to the `Provider`
protocol in `agentree/providers/base.py` and never names a provider.

```toml
opencode_cmd = "opencode"     # or a wrapper script, e.g. "~/bin/opencode-auto"
claude_cmd = "claude"          # PATH (+ Homebrew, Linuxbrew, ~/.local/bin, Nix), then ~/.claude/local
default_agent = "opencode"     # a provider id: `o` chooser / spawn dialog / `agentree spawn` default
```

Every provider has a `<id>_cmd`-style key (its descriptor's `command_key`); an unknown
`default_agent` is a config warning and opencode stays the default.

**`agentree --json` / the snapshot cache.** Every agent has the same keys: `provider`, `session_id`,
`status_source`, `status_quality` (`exact` | `approximate` | `unavailable`), `status_note` and
`meta` (provider-specific scalars, e.g. opencode's `{"port": 5000, "api_ok": true}`), next to
pid, cwd, status, title, … For older readers the legacy aliases are still written: `kind`
(= provider), `oc_session_id` / `cc_session_id` (the session id of an opencode / claude agent,
else null), `port` and `api_ok` (opencode's meta, else null / false). Reading an old cache: no
kind/provider means opencode; aliases that contradict the canonical keys are rejected.

**In the popup** all agents look the same (one `◆` icon coloured by status); the detail view
shows status, worktree, session id, context usage, pid and last message, plus a `note:` line
when the status is approximate or unavailable.

## Adding an agent provider

1. Write `agentree/providers/<id>.py` implementing `Provider` (base.py) and exporting
   `create_provider(spec)`. It owns process detection (`matches_argv`, `find_pids`), the launch
   (`resolve_command`, `build_launch` → `Launch(argv, session_id, setup, warnings, errors)`,
   `execute_setup` for its `SetupStep`s, `find_launched_pids`), status (`fetch_full`,
   `fetch_quick`, an optional watcher and its `refresh_policy`), `doctor_jobs` (a `versions`
   job and/or a section named after the id), hidden CLI commands (`run_internal`) and
   `demo_agents`.
2. Add one `ProviderSpec(id=, label=, module=, command_key=, default_command=,
   extra_command_dirs=, internal_commands=)` to `SPECS`. Ids, command keys and internal commands
   must be unique.

Nothing else changes: the config key, `default_agent`, `--agent`, the `o` chooser, the spawn
selector, remove's argv check, `agentree doctor` and `--demo` pick it up from the registry. Tests
inject providers with `registry.override(specs=, providers=)` (see `tests/_fake_provider.py`).

## Claude Code

- **Start:** `o` (new pane in a worktree's tab) and spawn with agent `claude`. Claude processes
  started any other way are shown too (detected by argv: `claude`, `…/claude/versions/<v>`,
  `node …/claude-code/cli.js`; `claude mcp`, `-p`, `--version` and the like are not agents).
  Claude has no port: jump goes to its zellij pane, as for any agent.
- **Hooks:** agentree starts claude with `--settings <state>/claude/settings.json --session-id <uuid>`.
  That file only adds command hooks (SessionStart, UserPromptSubmit, Pre/PostToolUse,
  PermissionRequest, Notification, Stop, SessionEnd). Claude merges hooks across settings sources,
  so your own settings and hooks keep working. Each hook runs `agentree claude-hook`, which reads the
  hook JSON on stdin and writes `<state>/claude/status/<session id>.json` (status, since, cwd,
  transcript, pid, zellij session/pane, notification text, last message). It always exits 0 and
  never prints, so a broken state dir cannot disturb Claude.
- **Status sources**, first available wins (`Agent.status_source`):
  1. `hooks`: the hook record (busy, idle, needs_input; idle for more than 30 min reads done);
  2. `registry`: Claude's own session registry `~/.claude/sessions/<pid>.json` (unofficial
     format, read defensively);
  3. `transcript`: a heuristic on the end of the session's transcript
     (`~/.claude/projects/<cwd>/*.jsonl`); approximate.
- **Limitations:** a claude started outside agentree has no hooks, so its status comes from the
  registry or the transcript. Claude sends no Stop hook when you interrupt it (Esc); agentree reads
  the transcript and corrects a stale busy / needs_input to idle. `/clear` and `/resume` switch
  the session inside one process; the newest hook record of that process wins.
