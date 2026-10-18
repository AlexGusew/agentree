# agentree

**One popup for all your zellij sessions, git worktrees and coding agents.**

If you run several AI coding agents (opencode, Claude Code) in parallel, each in its own git
worktree and zellij tab, it gets hard to tell what is where and which agent is waiting for
you. agentree is a fast, keyboard-driven popup that shows everything as one tree and jumps to
any of it:

```
▾ ● web-app    3 tabs ◆2 attached
    ⎇ main    root tab 1 clean
  ▾ ⎇ feat/sso-login    tab 2 ±6 ↑3
      ◆ Refactor token refresh    busy 2m
      ◆ Write refresh tests    idle
    ⎇ fix/flaky-e2e    tab 3 ±2 ↑1
    ⎇ chore/session-store    no tab ±3 ↑1
    ⎇ feat/billing-export    no tab clean ↓4
▾ ● dotfiles    1 tab ◆1 attached
  ▾ ⎇ main    root tab 1 ±1
      ◆ Migrate fish abbreviations    needs input
▾ ○ engine    2 tabs ◆2 detached 5h
    ⎇ main    root tab 1 clean
  ▾ ⎇ perf/simd-hash    tab 2 ±4 ↑2
      ◆ Profile hash table    done 3h
      ◆ Vectorise the probe loop    busy <1m
  ✕ scratch    exited 3d
▾ ⌂ Needs a home    1 worktree · 1 agent
    ⎇ agent/rate-limit    no tab ±2 ↑5
    ◆ Add rate limiter    busy · no pane
```

*(This is `agentree tree --demo --expand`. Try the real popup with sample data: `agentree --demo`.)*

## Features

- **One tree:** zellij sessions → git worktrees → agents and panes, with dirty / ahead / behind
  counts and live agent status (busy, idle, needs input, done).
- **Jump anywhere** with `↵`. agentree switches the zellij tab and pane and focuses the right
  kitty window. A detached or exited session is attached in a new kitty tab.
- **Spawn a workspace** with `^n` or `agentree spawn`: a new branch and worktree, `.env` files
  copied over, dependencies optionally installed, then a tab (or a new session) with an agent
  already started on your prompt. If any step fails, everything is rolled back.
- **Agent here** with `o` (a new agent pane in a worktree's tab), and **lazygit** with `g`
  (a floating pane).
- **Clean up:** remove a worktree (closing its tabs and stopping its agents, and deleting the
  branch if it's merged), kill an agent, rename or kill a session.
- **Live status:** opencode's event stream and Claude Code hooks update the popup as agents work.
- **Careful by design:** every action re-checks live state first and refuses rather than
  guess. `--dry-run` logs what would run. agentree makes no network requests besides
  `127.0.0.1`, and never runs `git fetch`, `pull` or `push`.
- **No dependencies:** Python standard library only. The popup paints its cached tree in about
  0.1 s, then refreshes in the background.

## Requirements

| | |
|---|---|
| Python | 3.14+ |
| [zellij](https://zellij.dev) | 0.45+ |
| [kitty](https://sw.kovidgoyal.net/kitty/) | with remote control enabled (see below) |
| git | any recent version |
| Agents (optional) | [opencode](https://opencode.ai) and/or [Claude Code](https://claude.com/claude-code) |
| [lazygit](https://github.com/jesseduffield/lazygit) (optional) | for `g` |

macOS is the primary, tested platform. Linux support is in progress.

## Install

With [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/):

```sh
pipx install --python python3.14 git+https://github.com/AlexGusew/agentree
# or
uv tool install --python 3.14 git+https://github.com/AlexGusew/agentree
```

Or build a single-file zipapp (fastest startup), installed to `~/bin/agentree`. Run the build
with Python 3.14+: the zipapp keeps using that interpreter.

```sh
git clone https://github.com/AlexGusew/agentree && cd agentree
python3.14 scripts/build.py --install
```

## Setup

**1. Let agentree talk to kitty.** In `kitty.conf`:

```conf
allow_remote_control socket-only
listen_on unix:/tmp/kitty.sock
```

kitty appends its pid (`/tmp/kitty.sock-<pid>`). agentree only uses sockets of that form that
belong to you and to a live kitty process.

**2. Bind the popup in zellij.** In `~/.config/zellij/config.kdl`, inside
`keybinds { shared_except "locked" { … } }`:

```kdl
bind "Alt w" {
    Run "agentree" {
        floating true
        close_on_exit true
        name "agentree"
        width "80%"
        height "80%"
        x "10%"
        y "10%"
    }
    SwitchToMode "normal"
}
```

Use an absolute path if `agentree` is not on the PATH that zellij sees. Pressing `Alt w`
again while the popup is open focuses it instead of opening a second one.

**3. Check everything.**

```sh
agentree doctor
```

It checks tool versions, the kitty sockets and config, the zellij sessions and keybind, your
agents and the agentree config. It exits non-zero if anything fails.

## Usage

Press `Alt w` in zellij. `?` shows all keys.

| Key | Action |
|---|---|
| `j` `k` / `↑` `↓` | move |
| `h` `l` / `←` `→` | fold, or go to parent / child |
| `␣` | toggle fold |
| `↵` | jump to the session, worktree, agent or pane |
| `o` | start an agent in this worktree's tab |
| `t` | open a tab for this worktree (or jump to its existing tab) |
| `g` | lazygit in a floating pane |
| `^n` | spawn a new workspace (branch + worktree + tab + agent) |
| `x` | remove (worktree, agent or session), with a confirmation listing the exact commands |
| `r` | rename the session |
| `/` | filter by text |
| `W` | cycle the worktree filter: all, with a tab, dirty, hidden |
| `A` `P` `D` | show or hide agents, panes, detached sessions |
| `R` | refresh now |
| `esc` | clear the filter, then close |
| `q` | close |

### Command line

```sh
agentree                        # the popup
agentree --demo                 # the popup with sample data, no commands run
agentree --dry-run              # actions only log what they would run
agentree spawn REPO BRANCH      # create a workspace from the shell (see --help)
agentree --json                 # the full snapshot as JSON, for scripts
agentree doctor                 # check the environment
```

`agentree spawn` takes `--prompt "…"`, `--agent opencode|claude`, `--session` (a new zellij
session instead of a tab), `--layout NAME`, `--base origin|head`, `--install`, `--no-env`,
`--no-switch` and `--dry-run`. `REPO` is a repository name under `repo_roots`, or a path.

## Agents

agentree finds agents by their processes, however they were started, and matches them to the
zellij pane and worktree they run in.

- **opencode:** status comes from the opencode server's local HTTP API and event stream.
  `opencode attach` clients are shown too.
- **Claude Code:** agents started by agentree (`o`, spawn) get exact status through hooks.
  agentree passes its own `--settings` file to `claude`, which only adds hooks. Claude merges
  hook settings, so your own settings and hooks keep working, and agentree never edits them.
  Claude processes started any other way are shown with an approximate status, read from
  Claude's session files.

Other agent tools can be added as a provider module: see
[docs/agent-providers.md](docs/agent-providers.md).

## Configuration

Everything is optional. agentree reads `~/.config/agentree/config.toml` (`$XDG_CONFIG_HOME` is
respected; `$AGENTREE_CONFIG` overrides):

```toml
# Repositories to scan for worktrees (globs).
repo_roots = ["~/Projects/*"]

# Where new worktrees go. {repo} = repo root, {name} = repo basename, {slug} = branch slug.
# A folder inside the repo is added to .git/info/exclude, so it never shows up as untracked.
worktree_template = "{repo}/.worktrees/{slug}"

# Prefix for branches created by spawn.
branch_prefix = "agent/"

# Agent commands, and which agent spawn and `o` preselect.
opencode_cmd = "opencode"
claude_cmd = "claude"
default_agent = "opencode"

# zellij layouts offered by the spawn dialog, and the layout for tabs opened with `t`.
layouts = ["agent", "default"]
# tab_layout = "agent"

lazygit_cmd = "lazygit"
accent = "#e5b454"

[default_filters]
worktree_mode = "all"   # all | with_tab | dirty | hide
agents = true
panes = false
detached = true

# Install command per lockfile, for spawn's optional install step.
[install_commands]
"pnpm-lock.yaml" = "pnpm install"
"Cargo.lock" = "cargo fetch"
```

Unknown keys and wrong types are reported by `agentree doctor`. They never stop agentree from starting.

| Environment variable | Effect |
|---|---|
| `AGENTREE_CONFIG` | config file path |
| `AGENTREE_STATE_DIR` | state directory (default `$XDG_STATE_HOME/agentree`, i.e. `~/.local/state/agentree`) |
| `AGENTREE_LIVE=0` | turn off live event streams (plain 2 s polling) |
| `AGENTREE_MOUSE=0` | turn off mouse support |
| `AGENTREE_SELF` | the command Claude Code hooks use to call agentree (default: the running executable) |

The state directory holds the snapshot cache, `ui.json` (remembered folds and filters),
`links.json` (which worktree belongs to which session, tab and agent), the action log (`log`,
every command agentree ran), the Claude Code hook settings and status (`claude/`), locks, and
the journals of interrupted spawns.

## Documentation

- [docs/design.md](docs/design.md): exactly what each action checks and does, its safety rules
  and rollback, and the zellij / kitty behaviour it relies on.
- [docs/agent-providers.md](docs/agent-providers.md): the provider interface, Claude Code
  details, and how to add an agent tool.
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and tests.

## License

[MIT](LICENSE)
