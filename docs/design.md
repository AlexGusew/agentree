# How agentree works

This is the detailed behaviour reference: what every action checks, in which order, and why.
For installing and day-to-day use, start with the [README](../README.md). Agent providers
(opencode, Claude Code, adding your own) are described in [agent-providers.md](agent-providers.md).

agentree never trusts its cached snapshot when it acts: every action re-reads the live state
of zellij, kitty, git and the agent processes, and refuses with a message rather than guess.

## Single instance

**One popup per session.** A session is identified by its canonical socket dir plus its name;
`<key>` below is the first 16 hex characters of the sha1 of that identity.

- **Claiming:** the popup holds an exclusive, non-blocking `fcntl` lock on
  `~/.local/state/agentree/instance-<key>.lock` for as long as it runs. Taking the lock is the claim.
  The kernel releases it when the process dies, so stale records never block.
- **Record:** `instance-<key>.json` next to the lock stores pid, process start time, session,
  socket dir and pane id.
- **Second start** (for example `Alt w` pressed again): agentree does not draw anything. It focuses the
  first popup's pane, using an explicit `--session` and `ZELLIJ_SOCKET_DIR`, but only when all of
  these hold:
  - the record matches this session;
  - the pane still exists in a fresh `list-panes`;
  - exactly one client is attached.

  Otherwise it prints a message and exits 0. `--dry-run` never focuses anything.
- **Outside zellij** (no `ZELLIJ_SESSION_NAME`) there is no single-instance check.
- **`--demo`** uses inert actions (`NullActions`): demo data never runs a command.

## Jump and open tab

| key | row | what happens |
|-----|-----|--------------|
| `↵` | session / worktree / agent / pane | jump there |
| `t` | worktree (incl. "Needs a home") | open a tab for it, or jump to the tab that already has it |

### Jump

The order is: zellij tab, then pane, then the kitty window (`go-to-tab-by-id`, `focus-pane-id`,
`kitty @ focus-window`). agentree never uses `switch-session`.

**Pane focus is read back.** In zellij 0.45.1, `focus-pane-id` exits 0 but does nothing for
tiled panes. agentree reads the pane the single client is focused on from `list-clients` (the
`ZELLIJ_PANE_ID` column). It never uses `list-panes`' `is_focused` for this.

`focus-next-pane` (checked live):
- walks the tiled panes of the active tab in order and wraps around;
- never leaves a floating layer that is shown, even with one floating pane;
- visits only the expanded pane of a stack and skips the collapsed ones.

Two zellij commands hide **the whole floating layer** of a tab (checked live): an explicit
`toggle-floating-panes`, and `focus-pane-id` on a tiled pane while a shown floating layer has
the focus. So, after `go-to-tab-by-id`, agentree reads the focus state **before any focus change**:
the client's pane (`list-clients`), a fresh `list-panes` (is the target tiled or floating,
which floating panes live in the tab, which one is agentree's own) and the tab's
`are_floating_panes_visible` (`list-tabs`). Then:

- **Already focused:** nothing else is sent.
- **Floating target** (for example `g`'s lazygit): `focus-pane-id`, which does focus floating
  panes, then read back. A floating target is never walked to.
- **Tiled target, no shown floating layer, or one without live floating panes:**
  `focus-pane-id`, then the walk below.
- **Tiled target, live floating panes in the tab, but their visibility can't be read** (the
  `list-tabs` query fails, the field is missing, or the tab is gone): nothing is sent, and agentree
  reports "could not determine floating layer visibility".
- **Tiled target, shown layer holding any floating pane other than agentree's own** (the user's,
  or a plugin): agentree sends neither `focus-pane-id` nor `toggle-floating-panes`, because either
  would hide them. It reports "floating panes are shown in this tab (terminal_N); not hiding
  them — hide them (Ctrl Alt f, or Alt p w) and retry". These are the `ToggleFloatingPanes`
  keys in `~/.config/zellij/config.kdl`.
- **Tiled target, agentree's own popup the only floating pane of the shown layer** (the
  `ZELLIJ_PANE_ID` of the agentree process, in the session agentree runs in): agentree records that it
  toggled and runs `toggle-floating-panes`. Focus then falls to the tab's tiled layer and the
  walk follows. If focus then fails (the walk, or `focus-pane-id` itself), agentree toggles the
  layer back, but only while the client is still on a tiled pane of the same tab and
  `list-tabs` explicitly reports the layer as hidden (an unreadable visibility means no toggle).

**The walk.** If the target is still not focused, agentree runs `focus-next-pane` until it is,
re-reading `list-clients` and `list-panes` before **every** step. There must still be exactly
one client, in the target tab, on a tiled pane, and the target must still be a tiled pane of
that tab. Any change (you switched tabs, showed floating panes, closed or moved the target, a
second client attached) stops the walk. It is bounded by (selectable tiled panes in the tab +
1) steps, so a stacked target the walk can't reach ends it. The whole read-back (focus,
toggle, walk) has one 3 s deadline.

When the target pane is not reached, an agent or pane jump returns **ok=False and keeps the
popup open** with "tab and window reached; could not focus pane terminal_N (<reason>)". The
zellij tab and the kitty window have been switched. Session and tab-only jumps don't read pane
focus back and are unchanged.

Before every action, agentree re-checks the target:

- **Session identity.** It runs `list-sessions` in every socket dir and refuses (with a "refresh"
  message where useful) when:
  - a name is live in more than one dir;
  - a session moved to another dir;
  - a session is gone;
  - a session's server restarted since the snapshot (it is younger than expected by more than 5 s).

  A cached exited session that is live again gets navigated to, not resurrected.
  "agentree runs inside the target" compares the canonical socket dir plus the name
  (`ZELLIJ_SOCKET_DIR` + `ZELLIJ_SESSION_NAME`).
- **Clients.** A failed `list-clients` refuses the action; it is never read as zero clients. Tab
  and pane navigation happens only with exactly one client, counted again right before acting.
  Otherwise agentree refuses and leaves the popup open with the message.
- **Target.** Against a fresh listing:
  - a worktree's tab must still hold a pane inside that worktree (the deepest worktree root wins);
  - an agent's pane must exist, and the agent process must be alive and still carry that
    `ZELLIJ_SESSION_NAME` / `ZELLIJ_PANE_ID`.

- **agentree runs inside the target session:** zellij navigation only. The popup closes when agentree exits.
- **The session has a kitty window** (corroborated by window title plus argv): agentree first re-checks
  that window with a fresh `kitty @ ls`. If the cached window is gone, agentree looks for the session's
  window again across the owned sockets. It then navigates in zellij and focuses the window.
  `focus-window` also selects the kitty tab, raises the OS window and activates kitty (checked on macOS).
- **Live session, no client attached:** agentree opens a new kitty tab running
  `zellij attach <S>` (with `--var agentree_session=<S>` and `ZELLIJ_SOCKET_DIR`). It waits up to 3 s
  for the client to appear, then navigates.
- **Exited session:** a new kitty tab runs `zellij attach <S>`, which resurrects it. There is no
  tab navigation.
- **Several clients attached:** `zellij action` acts on the "last active client", so agentree refuses
  tab and pane navigation and keeps the popup open with the message. A session row with
  several clients still just focuses the kitty window.
- **Refusals** (each shown as a message): the session's inventory is incomplete; the kitty
  window now shows a different session; there are several kitty instances and none of them
  hosts agentree.

agentree picks the kitty socket for new tabs this way. It uses `$KITTY_LISTEN_ON` if that is a
live, owned `/tmp/kitty.sock-<pid>`; otherwise the only such socket. If neither applies, it
refuses.

### Open tab (`t`) and `ensure_tab`

Target session: the row's session if it is live, otherwise the session agentree runs in.

The whole sequence lives in `tabs.ensure_tab`, which spawn and `o` reuse. It runs
under `~/.local/state/agentree/locks/tab-<key>.lock`:

1. List the session's tabs and panes fresh. Refuse if the listing is inconsistent.
2. If a terminal pane already has its cwd in the worktree (the deepest worktree root wins),
   use that tab.
3. If no client is attached, attach one in a new kitty tab, then list again: the duplicate check
   is repeated after attaching.
4. Count clients again, right before creating. Never create with zero clients.
5. Run `zellij action new-tab --cwd <worktree> --name <basename>`, adding
   `--layout <tab_layout>` if set in `config.toml`, and `--no-focus` when more than one client is
   attached. In that case agentree doesn't switch to the new tab.
6. Verify the printed tab id: it must not have existed before, must exist now, must have the
   requested name and must have a pane inside the worktree.

Then agentree navigates to the tab like a jump.

The lock only coordinates agentree instances. Tabs created outside agentree (the zellij UI, scripts) are
caught by the fresh listing, but one created between that listing and `new-tab` is not.

### Dry run

`agentree --dry-run` changes nothing. Read-only queries still run, because decisions depend on them.
Every side-effecting command goes to `~/.local/state/agentree/log` as a `DRY` line with the exact
command line, including `ZELLIJ_SOCKET_DIR=…`. The status line reads
`dry-run: N commands logged`. Real runs log every command with its exit code. The demo
(`--demo`) runs no commands at all.

## Spawn, remove, rename

### Spawn (`^n`, or `agentree spawn`)

```sh
agentree spawn <repo> <branch> [--base origin|head] [--session] [--layout L] [--prompt …]
          [--agent <provider id>] [--no-env] [--install] [--no-switch] [--dry-run]
```

`<repo>` is a repo name under `repo_roots` or a path. The form and the CLI share the same plan:
it lists the exact commands, warnings and blocking errors. Computing the plan has no side
effects and takes about 15 ms.

**Branch, base and path**
- The slug is the branch's last path component, cleaned up to `[A-Za-z0-9._-]`. The worktree
  path comes from `worktree_template` (default `{repo}/.worktrees/{slug}`).
- The base is origin's default branch (`refs/remotes/origin/HEAD`, cached per repo), falling back
  to `HEAD` with a warning.
- If the branch already exists and is checked out nowhere, agentree reuses it:
  `worktree add <path> <branch>`, with a warning.

**Blocking errors**
- invalid branch name, or a branch already checked out somewhere
- the worktree path already exists
- the repo or the base ref is missing
- tab target only: no live session for the repo ("choose new session"), several candidate
  sessions, or an incomplete tab/pane inventory
- session target only: the session already exists

**Steps** (each shown as its exact command; running state, output and rollback show in the progress view):
1. Append `.worktrees/` to `.git/info/exclude` if that path is not ignored yet.
2. `git -C <repo> worktree add -b <branch> <path> <base>`.
3. Copy the root checkout's top-level `.env*` files in-process (no `cp`). `.env.example`,
   `.env.sample` and `.env.template` are left out.
   - The source must be a regular file, not a symlink.
   - An existing destination (a tracked file or a symlink) is never overwritten. The preview
     predicts these skips with `git ls-tree`.
   - Files are created with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600, keeping the source's mtime.
     Each one is fingerprinted with sha256.
4. Install, using the command for the first lockfile found (`install_commands`). It runs in the
   worktree with a 600 s timeout. Its output streams into the progress view, and cancelling
   kills the whole process group.
5. The target:
   - **Tab**: `ensure_tab` in the repo's session. That is the session agentree runs in if it holds the
     repo, else the only live session holding the repo, else the one with the root checkout
     open, else the one named after the repo.
   - **Session**: zellij rejects `/` in session names, so the session is named `<repo>-<slug>`.
     agentree creates it with a client attached, as a kitty tab running
     `zellij -s <name> [-n <layout>]` (`--keep-focus` when not switching). A per-name lock is
     held from the "does it exist?" check until a client is attached.
6. Record it in `links.json` (`worktreePath`, `session`, `tabName`, `tabId`, `agentPids`). For a
   new session, `tabName` is its real first tab. agentree asks the chosen provider for up to 3 s for
   its agent processes in the new tab's panes and stores their pids (a provider that gives the
   launch a session id prefers the process carrying it, e.g. claude's `--session-id`). The record
   it replaced, if any, is remembered for rollback.
7. Switch to it: jump-style navigation, then the popup closes.

**Agent.** The form's `agent` selector (`--agent` on the CLI, one of the registered provider
ids; default: config `default_agent`) picks the provider that starts the agent. The selector
lists every provider; one whose command is not installed is marked unavailable, and choosing it
is a blocking error only if a command would actually be launched.

**Prompt.** The provider turns the request (prompt, layout, plan-time session id) into a launch
command, optional setup steps and warnings. When it returns a command, the layout is replaced by
a built-in agent layout (agent 65% | shell, pane named after the provider id): the pane's
`command` is the executable and `args` the rest. The preview shows the prompt as `<prompt>`.
- opencode: `opencode_cmd` with `args … "--prompt" "<prompt>"`. Without a prompt it starts
  nothing and the named layout (e.g. your `agent.kdl`) is kept.
- claude: `claude_cmd` (resolved like `o`, below) with
  `args … "--settings" "<state>/claude/settings.json" "--session-id" "<uuid>" ["--"] "<prompt>"`
  (`--` when the prompt starts with `-`). Its setup step writes the hooks settings file. With
  the `agent` layout and no prompt it also starts, without a first message.
- Setup steps are plain plan data (`{"kind": "provider_setup", "provider", "operation",
  "label", "payload"}`); they run after the worktree / env / install steps and before the
  tab or session step (skipped under `--dry-run`).
- Any other layout without a prompt starts no agent (the provider's warning says so). A missing
  agent command, when one would be launched, is a blocking error. The session id is generated at
  plan time and reused at execute time.

The prompt is escaped for KDL. For a
tab it goes through `new-tab --layout-string`; for a session, through a layout file under the
state dir. That file has a unique name, is created exclusively with mode 0600, and is deleted
once the session runs (or on rollback). Prompts with quotes, newlines and backslashes were
checked live and arrived unchanged.

**Locks and journal.** A whole spawn runs under the repo's creation lock
(`locks/repo-<sha1>.lock`); worktree removes take the same lock. Before each create step, agentree
records what it is about to create, with identities, in `<state>/journal/<run-id>.json`, and
updates the record afterwards. If that file can't be written, the spawn stops before creating
the resource. The id that `new-tab` prints is journaled before any further query. The
`links.json` record about to be replaced is written to the journal under the links lock before
the swap (`state.swap_link_journaled`), so a crash in between can be recovered from the journal. The file is deleted when the run ends cleanly. It stays when
something had to be kept, or after a crash: `state.list_journals()`, for `agentree doctor`.

A failed or timed-out `worktree add -b` is inspected: a branch at the base commit and a worktree
registered at the path are treated as this run's. A tab id that zellij printed is kept even when
verification fails.

**Rollback** runs on failure or cancel, in reverse order, from the journal. It re-checks each
resource's identity first. Anything uncertain, and any cleanup that fails, stays an unresolved
journal entry: the journal file is kept, and the step shows as failed with a `kept:` or
`uncertain:` note. A resource that is already gone counts as resolved.
- **links.json**: the previous record is put back, under the links lock, but only if the current
  record is still the one this run wrote.
- **Tab**: closed with `close-tab-by-id` only if it still has the expected name, all its live
  terminal panes are inside the worktree (otherwise the mixed panes are reported), it is in the
  same session incarnation, and agentree's own pane is not in it.
- **Session**: `kill-session` + `delete-session` only if it is still the incarnation this run
  created (its created-age matches), all its live panes are inside the worktree, and agentree doesn't
  run in it. Afterwards a fresh listing must no longer show it, otherwise it is kept. A session
  that was launched but never seen is looked up again. It is rolled back only if found in the
  expected socket dir, younger than the launch, and passing the same pane check; otherwise it
  stays uncertain.
- **Kitty window**: closed only if it still carries `agentree_session=<name>` and its session was
  rolled back (it usually closes by itself). A failed close is kept.
- **Worktree**: `git worktree remove --force` only if every file in it is either tracked and
  unchanged, or one agentree created: a copied `.env` file, or a file present right after
  `worktree add`, in both cases with an unchanged fingerprint. Ignored files count too
  (`git status --porcelain=v2 --ignored=traditional -uall`). Directories, such as nested repos,
  are fingerprinted recursively without following symlinks: a sorted list of
  (path, type, size, mtime, sha256 or link target). Past 20k entries or 2 s, or on any other
  file type, the fingerprint is unknown, and an unknown fingerprint never matches, so the
  worktree is kept. Otherwise the step reads
  "kept <path>: contains files not created by agentree".
- **Branch**: only if this run created it, its worktree was removed, and it still points at the
  base: `git update-ref -d refs/heads/<b> <base>` (an atomic compare-and-delete).

The exclude line stays.

**Network.** agentree itself never makes non-localhost requests: only `127.0.0.1` to opencode,
never `git fetch/pull/push`. The install step is off by default; when enabled, the preview warns
that it may download packages.

### Remove (`x`)

Every remove goes through a confirmation that lists the exact commands, and is checked again
right before it runs.

**Worktree.** The root checkout is refused. So is a worktree with a tab that would be closed
while holding agentree's own pane: "agentree is running in a tab of this worktree; open agentree from another
tab (Alt w) to remove it". The plan records what you approve: canonical path, current branch,
HEAD, branch tip, and the default ref with its sha. Execute re-reads all of them and refuses if
anything changed ("… changed since confirmation — re-run x"). It runs under the repo lock. The
steps:
1. Close every tab whose panes are all inside the worktree. Before closing, agentree re-checks the
   session's socket dir and incarnation, and that agentree's own pane is not in the tab. A changed
   session stops the whole remove. Tabs with panes elsewhere stay open, with a warning.
2. Stop agents in the worktree with `kill -TERM`, after checking they are still the same
   process. The snapshot's `Agent.start_time` is authoritative and is never replaced by the live
   process's start time. If the snapshot has no start time but the live process has one, agentree
   refuses: "agent identity unknown in this snapshot — refresh (R) and retry". If both are
   known, they must match. Also checked: a command line that the agent's provider recognises as
   its own (`matches_argv`; an opencode agent whose pid now runs claude is refused, and the
   reverse), and a cwd still inside the worktree. A record without a provider (older plans and
   caches) means opencode; an agent of an unknown provider is refused ("unknown agent provider").
3. `git worktree remove`.
4. If the approved tip is merged into the default ref, agentree runs
   `git update-ref -d refs/heads/<branch> <approved tip>`, after checking `merge-base
   --is-ancestor` again. The confirmation shows this exact command.
5. Drop its `links.json` record.

If the worktree is dirty, you must type the branch name, and the remove uses `--force`. The branch
must still equal what you typed. A worktree that becomes dirty after you confirm is refused.

**Agent.** `kill -TERM` after the same identity check, which also requires the same pane and
session. For an agent with no pane and no session, the live cwd must equal the snapshot's, both
when planning and when executing.

**Session.** A live session gets `zellij kill-session`, an exited one `zellij delete-session`.
agentree never kills the session it runs in, and refuses ambiguous names (live in several socket dirs)
and sessions restarted since the snapshot.

Pane and group rows: "nothing to remove".

### Rename (`r`)

`zellij --session <old> action rename-session <new>`. The new name must:
- contain no `/`, spaces or control characters;
- be short enough for macOS' 104-byte socket path;
- not exist in any socket dir, live or exited.

Exited sessions are refused. `links.json` records follow the rename; if writing them fails, the
rename is reported as a partial success. Renaming the session agentree itself runs in closes agentree,
because its `ZELLIJ_SESSION_NAME` and single-instance claim refer to the old name. Until it
exits, agentree keeps treating the new name as its own session.

Known limitation: kitty windows opened with `zellij attach <old>` keep the old name in their
command line, so after a rename agentree no longer recognises them as the session's window. Jump
then opens or uses another window.

## New agent and lazygit

### New agent (`o`)

`o` ("agent here") opens a new agent pane in the worktree's tab and jumps to it. Works on a
worktree row (including "Needs a home" worktrees and the root checkout), and on an agent or pane
row, where it uses the deepest known worktree that holds the row's cwd.

Which agent: agentree asks every provider whether its command is installed. None → "No agent command
found; run agentree doctor". One → it is launched directly. Several → a small "Agent here" chooser
(preselects `default_agent` if installed, else the first; `↑`/`↓`/`j`/`k`, `↵` launches, `esc`
cancels).

1. Session: the same as for `t`. That is the row's session if it is live; otherwise the session
   agentree runs in. Its identity is checked fresh.
2. Clients: if more than one client is attached, agentree refuses before creating anything, because
   zellij would act on an arbitrary client.
3. `ensure_tab` finds or creates the worktree's tab. It holds the lock, attaches a kitty client
   if none is attached, and re-validates.
4. agentree takes the session's action lock (`locks/tab-<key>.lock`, the one `ensure_tab` uses) and
   holds it through steps 4–7. A fresh listing must show that the tab id still exists and still
   holds the worktree. The pane ids listed now are remembered. Clients are re-queried right
   before creating; exactly one is required.
5. `new-pane --tab-id <id> --cwd <wt> --name <provider id> -- <launch argv>`, the argv built by
   the provider with its command resolved to an absolute path: opencode `<opencode_cmd>`; claude
   `<claude_cmd> --settings <state>/claude/settings.json --session-id <new uuid>`. The provider's
   setup steps run first (claude: the settings file is (re)written). The pane is placed
   explicitly in that tab, whatever tab is active. An unknown `--tab-id` still exits 0 and
   prints an id, which is why the id was validated in step 4.
6. The pane id that new-pane prints (`terminal_<N>`) must not be in the list from step 4. A
   fresh listing must show it in the requested tab, with a cwd inside the worktree. No printed
   id, or anything else, is reported, and nothing is focused or recorded.
7. Focus: `go-to-tab-by-id`, then the read-back of "Pane focus is read back" (focus state read
   before any focus change; never hides your floating panes for a tiled target; bounded,
   re-validated `focus-next-pane` walk). The session lock is then released, and agentree focuses the
   kitty window and closes. A `--tab-id` pane doesn't get focus by itself, and `focus-pane-id`
   doesn't move focus to a tiled pane, so the walk is what normally gets there. If focus fails,
   the result is ok with the popup kept open and a refresh: "agent pane created in tab X but
   focus failed: could not focus pane terminal_N (<reason>)".
8. `links.json`: one read-modify-write under `links.lock` (`state.merge_link`), which keeps
   fields agentree doesn't touch. `agentPids` is best effort: agentree asks the provider, for up to 3 s,
   for its process whose `ZELLIJ_PANE_ID` is the new pane; for claude the process carrying the
   new `--session-id` is preferred. Panes and pids already recorded are kept.
   - **No record, or one of this session** (same name, and the same `socketDir` if the record
     has one): it gets the session, `socketDir`, `sessionStarted` (the session's start time,
     from `list-sessions`), the tab and the pane (`agentPanes`).
   - **A record of another session B:** agentree checks whether B is this session renamed. That
     needs all of: B is live in no socket dir; the record's socket dir is this session's (its
     `socketDir`, or else the `ZELLIJ_SOCKET_DIR` of a still-running agent it lists); and the
     same incarnation. For the incarnation, the stored `sessionStarted` must match this
     session's start within 5 s. Records without `sessionStarted` instead need a listed agent
     that still carries `ZELLIJ_SESSION_NAME=B` and runs in a live pane of this session. Such
     an agent counts only if its process start time is known.

     The check returns its evidence: the record's `session`, `socketDir` and `sessionStarted`
     exactly as stored, plus the agents it relied on, each with its start time. Under the
     lock, the record must still carry those same three fields, and each of those agents must
     still be in `agentPids` with an unchanged start time. Only then is the record moved to
     the new name and the agent added. Otherwise the record is left alone, and the result ends
     with "link changed concurrently; agent not recorded".
   - **Otherwise** the record stays exactly as it is, and the result ends with "link kept for
     B; agent not recorded".

The lock only serialises agentree instances. What you do in zellij meanwhile (switching tabs, closing
panes) is outside it. That is why placement uses an explicit `--tab-id`, and why every step
re-validates against a fresh listing.

Dry run logs `new-pane --tab-id …` (plus `new-tab` when the tab would be created) and stops
there.

### lazygit (`g`)

The command comes from config `lazygit_cmd` (default `"lazygit"`), resolved on PATH plus a few
common install dirs (Homebrew, Linuxbrew, `~/.local/bin`, Nix). If it is missing, agentree shows
`lazygit not installed — see https://github.com/jesseduffield/lazygit#installation (or set lazygit_cmd in ~/.config/agentree/config.toml)`
and touches nothing. Otherwise the steps are the same as for `o`, with
`new-pane --tab-id <id> --cwd <wt> --name lazygit --floating --close-on-exit -- <lazygit>`.
Verification also requires the pane to be floating. Then agentree jumps to it (`focus-pane-id` does
focus floating panes) and closes. A failed focus reads "lazygit pane created in tab X but focus
failed: …" and keeps the popup open. Nothing is recorded in `links.json`.

```toml
lazygit_cmd = "lazygit"          # or a path / a command with arguments
```

## zellij 0.45.1 / kitty 0.48.2 behaviour (verified live)

- **`focus-pane-id` on a tiled pane** exits 0 and changes nothing (checked with `list-clients`
  and by typing into the focused pane). It does focus floating panes. See "Pane focus is read
  back" under Jump for what `focus-next-pane` does.
- **`new-pane --tab-id <id>`** places the pane in that tab, but the pane doesn't get focus, even
  in the active tab. With an unknown id it still exits 0 and prints an id for a pane that never
  appears. `o`/`g` use it for explicit placement and focus the pane separately.
- **`toggle-floating-panes`** hides or shows the whole floating layer of the tab.
- **`focus-pane-id <tiled pane>` while a shown floating layer has the focus** hides that layer
  (all its floating panes, not only agentree's) and focuses the tab's last tiled pane, not the
  target. That's why agentree reads the focus state first and doesn't send `focus-pane-id` for a
  tiled target while other floating panes are shown (see "Pane focus is read back").
- **`list-panes` `is_focused`** is per tab and can be true for several panes at once. The pane a
  client actually focuses is in the `ZELLIJ_PANE_ID` column of `list-clients`.

- **`new-tab` with no client attached** fails internally ("Not enough room for panes"), yet
  still prints a tab id. The next `attach` then panics the server
  ("failed to attach client to tab with index 1"). So agentree never creates tabs in a client-less
  session.
- **`go-to-tab-by-id <unknown>`** exits 0 and does nothing. **`focus-pane-id`** doesn't switch
  tabs. So agentree re-checks the target tab and pane against a fresh listing before navigating.
- **`focus-pane-id` on an already-focused pane** returned exit 0 in every live test. The
  documented exit-2 `Pane Terminal(N) is already focused` is still accepted, but only for the
  requested pane.
- **Window titles of new clients:** zellij sets the terminal title (`<session> | <pane title>`)
  only when a pane title changes. A freshly attached client keeps kitty's default title
  (`zellij`). So kitty windows that agentree launches carry the user var `agentree_session=<S>`. A window
  whose argv and user var both name S, and whose title names no session, counts as hosting S.
- **`list-sessions` inherits the caller's client env.** Run from inside a session (with
  `ZELLIJ`, `ZELLIJ_SESSION_NAME`, `ZELLIJ_PANE_ID` set), `ZELLIJ_SOCKET_DIR=<other dir> zellij
  list-sessions -n` lists the caller's session as live `(current)` in that dir too, even though
  `<dir>/contract_version_1/` has no socket for it. Without those variables it is listed EXITED.
  So the snapshot and `agentree doctor` run `list-sessions` with them removed (`env -u`). They also
  count a session as live in a dir only if `<dir>/contract_version_*/<name>` is a socket. The
  "live in two socket dirs" warning therefore only appears for real duplicates.
- **Background sessions created with a layout** (`attach --create-background S options
  --default-layout x.kdl`) work. Their tabs and cwds exist before any client attaches.

## Live status and `agentree doctor`

### Live agent status

The popup follows each opencode server's event stream (`GET /event?directory=<cwd>` on
`127.0.0.1:<port>`, one daemon thread per port; `agentree/providers/_opencode_events.py`) instead of waiting for
the next poll. Status events (`session.status`, `session.idle`, `session.updated`, `session.*`
create/delete/error/compacted, `permission.*`, `question.*`, `server.connected`) trigger an
**agents-only refresh** (`gather.gather_agents`, ~60–110 ms). It re-queries only the opencode
processes already shown (no `ps`) and the pane titles of their zellij sessions, then relinks them
with the zellij / git / kitty inputs of the last full refresh. Streaming noise (text deltas,
message parts, heartbeats) is ignored.

- Agents-only refreshes run at most every 150 ms and never overlap a full refresh: a request that
  arrives meanwhile waits for it to finish.
- Full refresh (zellij, git, kitty, new agents) runs every **10 s** while every watched agent's
  stream is connected. It runs every **2 s** while nothing is watched or any stream
  is down. Dropped streams reconnect with backoff (0.5 s → 5 s) and are given up once the agent's
  port is gone.
- New agents show up with the next full refresh, or right away after an action that refreshes.
- **`opencode attach <url> [--session <id>] [--dir <dir>]` clients** are agents too. Their port is
  the server's (often another agent's TUI with its embedded server), their cwd is `--dir` (else
  their own cwd). Status, title and last message come from their `--session` only
  (`GET /session/<id>`, its `/session/status` entry, its or its subagents' permission/question
  requests), not from the server's other sessions. If the pane title shows another session (the
  user switched inside the TUI), or there is no id (`-c`, `--fork`), the pane title picks the
  session. They share their server's single event stream: a TUI and its attach clients are one
  stream per port, and events are routed to them by `sessionID`. That stream is
  `/global/event` when they work in different directories. An attach client counts as covered
  for the 10 s cadence when that stream is up. A URL to a non-local host (anything but
  `localhost`, `127.0.0.1`, `::1`) is unsupported: a warning, no status.
- `AGENTREE_LIVE=0` turns event streams off (plain 2 s polling). The event module is imported only
  after the first frame, so startup time is unchanged.

### `agentree doctor`

Read-only environment check, finishes in under 2 s; exit 1 if any line is FAIL. Sections:

- **versions:** zellij (≥ 0.45), kitty, the providers' own version lines (opencode), git,
  python, lazygit (missing → `g` disabled).
- **kitty:** owned sockets and `kitty @ ls` on each; `allow_remote_control` and `listen_on` in
  kitty.conf (includes followed).
- **zellij:** live sessions per socket dir (socket-verified), names live in two dirs, the agentree keybind in
  config.kdl, configured layouts.
- one section per provider, in registry order, from its `doctor_jobs` (a provider that cannot
  be loaded, or whose checks crash, is a FAIL line in its section):
- **opencode:** `opencode_cmd`; per running server `/doc` and the event stream (once per port);
  attach clients as `pid N: attach → :5001 (session ses_…)`, without probing that port again; the
  effective live-status mode (`event streams on N/N servers`, or polling).
- **claude:** `claude_cmd` (resolved path), the Claude config dir (`$CLAUDE_CONFIG_DIR` or
  `~/.claude`), the hooks settings dir `<state>/claude`, and the running claude agents with how
  many of them report through hooks.
- **config:** config file and its warnings, repo roots, worktree template, state dir,
  `~/bin/agentree` and PATH, leftover spawn journals (`<state>/journal/*.json`, an interrupted spawn).

```sh
agentree doctor
```
