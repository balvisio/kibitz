# kibitz

A sidecar reviewer agent for Claude Code and Codex. Splits your tmux window, runs the *other* agent in the new pane, and forwards each user/assistant exchange from the host to the reviewer via a Claude Code Stop hook. The reviewer sees the discussion in near real time; by convention it stays quiet and only replies when the user asks it to.

Named after the Yiddish *kibbitzer* — the spectator who can't resist offering an opinion.

## What it does

- `kibitz start` splits the current tmux window (vertical divider), launches the reviewer agent in the new pane, labels the pane via smux (`codex` or `claude-reviewer`).
- A Claude Code `Stop` hook extracts the latest user + assistant turn from the session transcript and forwards them to the reviewer pane using `tmux-bridge`.
- The reviewer sends messages back via `kibitz send "<text>"`, which prepends a minimal `[kibitz from:<agent>]` header (e.g. `[kibitz from:codex]`) so the host agent can tell them apart from real user input.
- `kibitz stop` / `kibitz restart` / `kibitz status` manage the pane lifecycle.
- `kibitz uninstall` tears everything down cleanly.

### Behavior conventions

- **Reviewer is read-only by default.** The sidecar is meant to watch and comment, not drive. It should only message the host back when the user explicitly asks it to relay something.
- **Host can tell who typed what.** Anything that arrives at the host with a `[kibitz from:<agent>]` header came from the reviewer (emitted by `kibitz send`). Anything without it is real user input.
- **Slash commands are not forwarded.** If the most recent user turn starts with `/` (like `/clear`), the Stop hook skips forwarding that exchange.
- **Recursion-safe.** If the hook runs inside a pane already labeled `codex` or `claude-reviewer`, it no-ops — so nesting sessions doesn't create a forwarding loop.

### Directives

Append one of these tokens to the end of your message to change what the reviewer sees for that turn. Tokens are case-sensitive and must be preceded by whitespace (or stand alone as the whole message). Non-trailing occurrences are ignored.

| You type                  | Submit-time                  | After Claude's reply           |
|---------------------------|------------------------------|--------------------------------|
| `hello`                   | —                            | forward USER + CLAUDE          |
| `hello /mute`             | —                            | nothing                        |
| `hello /dup`              | forward USER (stripped)      | nothing                        |
| `/mute` (bare)            | —                            | nothing                        |
| `/dup` (bare)             | nothing                      | nothing                        |

- `/mute` — skip forwarding this exchange entirely. Useful for side chatter you don't want the reviewer to spend context on.
- `/dup` — send your message to the reviewer immediately (via a `UserPromptSubmit` hook), and *don't* forward Claude's reply. Useful when you want the reviewer to work on the same problem in parallel rather than critique the answer. If the submit-time forward fails (transport glitch, reviewer pane gone), the turn isn't silently retried at Stop time — errors land in `~/.claude/kibitz-hook.log` and you can resend with `/dup` manually.

Claude itself still sees the `/mute` or `/dup` suffix in your prompt — Claude Code hooks can observe a prompt but can't rewrite it in place. Treat the trailing directive as benign noise; the model ignores it.

## Requirements

- Linux (host detection reads `/proc`; macOS fallback is explicit-arg-only)
- tmux 3.2+
- `python3` on PATH
- `curl` or `wget` (for install)
- One of `codex` or `claude` CLIs on PATH for whichever agents you want to run

## Install

One-liner (no checkout needed):

```bash
curl -fsSL https://raw.githubusercontent.com/balvisio/kibitz/main/kibitz | bash -s install
```

Or equivalently:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/balvisio/kibitz/main/kibitz) install
```

When invoked without a local file on disk, `install` fetches the repo tarball into a tempdir and runs from there. Override the source with `KIBITZ_REPO` / `KIBITZ_BRANCH` env vars.

From a checkout of this repo:

```bash
./kibitz install
```

What install does:

1. Verifies `python3` is on PATH.
2. Copies `scripts/tmux-bridge`, `kibitz`, and the `hooks/kibitz_hook_*.py` scripts into `~/.local/bin/`.
3. Merges the Stop and UserPromptSubmit hook entries from `hooks/kibitz_hook.json` into `~/.claude/settings.json` (idempotent — won't duplicate on reinstall, preserves any other hooks you have).
4. Adds `~/.local/bin/` to `PATH` in your shell rc (`.zshrc` / `.bashrc` / `.profile`) if it isn't already.
5. Warns if `codex` or `claude` binaries are missing (non-fatal).

`tmux-bridge` is vendored from [`ShawnPana/smux`](https://github.com/ShawnPana/smux) under `scripts/`.

After install, open a new shell (or `source` your rc) so `PATH` picks up the new entries.

## Usage

All commands work whether you invoke `./kibitz` from the repo or `kibitz` from PATH after install.

```bash
# Start a sidecar. Auto-detects the host agent and launches the other one.
# Explicit override: `kibitz start codex` or `kibitz start claude`.
kibitz start

# Stop the sidecar (kills the labeled pane).
kibitz stop

# Show whether a sidecar is running, and where.
kibitz status

# Stop + start, optionally switching agents.
kibitz restart claude

# Remove the Stop hook entry and delete all installed scripts.
kibitz uninstall
```

### Talking to the reviewer

`tmux-bridge` is the communication layer. Labels are `codex` or `claude-reviewer` depending on which agent is running.

```bash
tmux-bridge read codex 30               # read last 30 lines of the reviewer pane
tmux-bridge type codex "summarize"      # type text into it (no Enter)
tmux-bridge keys codex Enter            # press Enter in the reviewer pane
```

## File layout

```
kibitz/
├── kibitz                      # the management script (bash)
├── hooks/
│   ├── kibitz_hook_stop.py                 # Stop hook — forwards user/assistant exchange
│   ├── kibitz_hook_user_prompt_submit.py   # UserPromptSubmit hook — implements `/dup`
│   ├── kibitz_hook_common.py               # shared helpers imported by both hooks
│   └── kibitz_hook.json                    # settings.json fragment `install` merges in
├── scripts/
│   └── tmux-bridge             # vendored from ShawnPana/smux
├── LICENSE
└── README.md
```

After install, the runtime copies land in `~/.local/bin/`:

```
~/.local/bin/
├── kibitz
├── kibitz_hook_stop.py
├── kibitz_hook_user_prompt_submit.py
├── kibitz_hook_common.py
└── tmux-bridge
```

And hook entries are added to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "$HOME/.local/bin/kibitz_hook_stop.py" }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "$HOME/.local/bin/kibitz_hook_user_prompt_submit.py" }
        ]
      }
    ]
  }
}
```

## Troubleshooting

- **`tmux-bridge not found`** — run `kibitz install`, or check that `~/.local/bin` is on `PATH`.
- **Hook not forwarding** — check `~/.claude/kibitz-hook.log`. The hook always exits 0 (so it can't block your session), and routes errors there.
- **`couldn't detect host agent`** — host detection is Linux-only (`/proc/$PPID/exe`). On macOS, or when running through an unusual wrapper, pass the agent explicitly: `kibitz start codex`.
- **Stale reviewer pane** — if `kibitz status` reports a pane that no longer exists, something killed the pane outside of kibitz. Run `kibitz stop` to clear the label, then `kibitz start`.

## Uninstall

```bash
kibitz uninstall
```

Removes the hook entry from `~/.claude/settings.json` (preserving any unrelated hooks you added) and deletes `kibitz`, `kibitz_hook_stop.py`, and `tmux-bridge` from `~/.local/bin/`. Leaves `PATH` edits in your shell rc alone.
