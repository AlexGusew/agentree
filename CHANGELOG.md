# Changelog

## 0.1.0 — unreleased

First public release.

- Popup tree of zellij sessions, git worktrees, agents and panes, with filters and a detail view.
- Jump to any row (zellij tab and pane, kitty window), and open a tab for a worktree.
- Spawn a workspace (branch, worktree, `.env` copy, optional install, tab or session, agent),
  with a journal and rollback.
- Remove worktrees, agents and sessions; rename sessions.
- `o` starts an agent pane in a worktree, `g` opens lazygit.
- Agent providers: opencode (live status over its event stream) and Claude Code (status
  through hooks).
- `agentree doctor`, `--dry-run`, `--demo`, `--json`.

### Migrating from `wsm`

Only relevant if you used the pre-release `wsm`. The project was renamed from `wsm` to
`agentree`, and every name changed with it:

| Before | After |
|---|---|
| `wsm` command, `~/bin/wsm` | `agentree`, `~/bin/agentree` |
| `~/.config/wsm/config.toml` | `~/.config/agentree/config.toml` (now honours `$XDG_CONFIG_HOME`) |
| `~/.local/state/wsm/` | `~/.local/state/agentree/` (now honours `$XDG_STATE_HOME`) |
| `WSM_CONFIG`, `WSM_STATE_DIR`, `WSM_LIVE`, `WSM_MOUSE`, `WSM_SELF` | `AGENTREE_…` |

```sh
mv ~/.config/wsm ~/.config/agentree
mv ~/.local/state/wsm ~/.local/state/agentree
rm ~/bin/wsm && python3 scripts/build.py --install
```

Then:

- Point the zellij keybind at `agentree` (and rename the pane from `wsm` to `agentree`).
- Restart running Claude Code agents that agentree started. Their hooks still call `wsm claude-hook`.
- kitty windows that `wsm` opened carry the user var `wsm_session`. agentree may not recognise
  them as a session's window until zellij sets the window title. Close them, or let agentree
  open new ones.
- The default `opencode_cmd` is now `opencode` (it was `~/bin/opencode-auto`). If you use a
  wrapper script, set `opencode_cmd` in the config.
- New worktrees now go to `{repo}/.worktrees/{slug}` (it was `{repo}/.slim/worktrees/{slug}`).
  Existing worktrees keep working: agentree finds them through `git worktree list`. To keep
  the old location, set `worktree_template = "{repo}/.slim/worktrees/{slug}"`.
