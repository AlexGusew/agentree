# Contributing to agentree

Thanks for your interest! Bug reports, ideas and pull requests are welcome.

## Reporting a bug

Please include:

- the output of `agentree doctor`;
- your OS and the versions of zellij, kitty and the agent tool involved;
- what you did, what you expected and what happened. For a failed action, the matching lines
  of `~/.local/state/agentree/log` (every command agentree ran, with its exit code) help a lot.

## Development setup

agentree uses only the Python standard library, so there is nothing to install besides
Python 3.14+.

```sh
git clone https://github.com/AlexGusew/agentree && cd agentree
python3 -m agentree --demo          # run from the source tree, with sample data
python3 -m agentree                 # run against your real sessions
python3 scripts/build.py            # build the zipapp: dist/agentree
```

Use `--dry-run` while you work on actions: read-only queries still run, and every command that
would change something is written to the log as a `DRY` line instead.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The unit tests don't need zellij, kitty or any agent. They use fakes and fixtures recorded from
real tools (`tests/fixtures/live/`). If you re-record fixtures with
`scripts/capture_fixtures.py`, check them first: they contain real names, paths and agent
conversations from your machine. Sanitize them before you commit.

### Live verification

Some behaviour can only be checked against real zellij and kitty. The scripts below drive
the real actions, but only on disposable resources: sessions, scratch repos and kitty windows
that a run creates with its own random id. A run aborts if any of those names already
exists, and cleans up everything when it ends (`--keep` keeps them for inspection).

| Script | Checks |
|---|---|
| `scripts/live_jump.py` | jump across kitty tabs and to a detached session; `t` twice (no duplicate tab); dry run; pane focus read back through `list-clients` (tiled, stacked and floating cases, including never hiding the user's floating panes); the `Alt w` popup geometry, single-instance hand-off, `q` and Enter → jump |
| `scripts/live_spawn.py` | spawn into a tab and into a new session with a prompt; rollback after an install failure and after a late failure; remove a clean or dirty worktree; kill an agent; rename and kill a session |
| `scripts/live_agent_pane.py` | `o` dry run; `o` into an existing tab while another tab is active, and creating the tab first; `g` opening and focusing a floating lazygit pane; `g` with a missing binary |
| `scripts/live_claude.py` | spawn with agent `claude` (a fake binary records its argv; the real CLI never runs), the generated hooks, and the `busy` status showing up in `agentree --json` |

`live_spawn.py` runs from a plain kitty window; the other three run from a kitty window,
plain or inside zellij. They use temporary
`AGENTREE_STATE_DIR` and `AGENTREE_CONFIG` values, so your real state is never touched.

`scripts/bench_startup.py` times the zipapp from spawn to its first painted frame (target:
under 150 ms).

## Guidelines

- **Standard library only.** No third-party runtime dependencies.
- **Never guess.** Actions re-validate against fresh live state right before they change
  anything, and refuse with a clear message when something doesn't match. Read
  [docs/design.md](docs/design.md) before changing an action: it explains each check and the
  zellij / kitty quirk behind it.
- **Keep startup fast.** Heavy imports stay lazy (see `agentree/cli.py`). Run the startup bench
  if you touch the TUI path.
- **Agent-specific code lives in a provider** (`agentree/providers/`), never in core code. See
  [docs/agent-providers.md](docs/agent-providers.md) for adding a new agent tool.
- Add or update tests with every change, and update the docs when behaviour changes.
