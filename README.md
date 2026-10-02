# kibitz

A sidecar reviewer agent for Claude Code and Codex. Splits your tmux window, runs the *other* agent in the new pane, and forwards each user/assistant exchange from the host to the reviewer via a Claude Code Stop hook. The reviewer sees the discussion in near real time; by convention it stays quiet and only replies when the user asks it to.

Named after the Yiddish *kibbitzer* — the spectator who can't resist offering an opinion.

## What it does

- `kibitz start` splits the current tmux window (vertical divider), launches the reviewer agent in the new pane, labels the pane via smux (`codex` or `claude-reviewer`).
- A Claude Code `Stop` hook extracts the latest user + assistant turn from the session transcript and forwards them to the reviewer pane using `tmux-bridge`.
- The reviewer sends messages back two ways, both prepend a `[kibitz from:<agent>]` header so the host can tell them apart from real user input:
  - `kibitz send "<text>"` — send custom text.
  - `kibitz relay [text]` — forward the agent's own last assistant reply verbatim to its counterpart pane. With text, the reply goes out framed as a `CLAUDE:` (or `CODEX:`) block followed by a `USER:` block holding the text — your own note or question about that reply. From a claude host pane the relay is sent by the Claude `Stop` hook when the current turn ends (one attempt); it sends the reply that was shown above your input when you ran the command, so rewinding first is honored. From a codex reviewer pane it forwards codex's last reply immediately, read from the `codex-<thread>.msg` stash the codex `Stop` hook writes under `$XDG_CACHE_HOME/kibitz/` keyed on `CODEX_THREAD_ID`. No dedupe — running `relay` twice on the same turn sends twice.
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
| `hello /tee`              | forward USER (stripped)      | nothing                        |
| `/mute` (bare)            | —                            | nothing                        |
| `/tee` (bare)             | nothing                      | nothing                        |

- `/mute` — skip forwarding this exchange entirely. Useful for side chatter you don't want the reviewer to spend context on.
- `/tee` — send your message to the reviewer immediately (via a `UserPromptSubmit` hook), and *don't* forward Claude's reply. Useful when you want the reviewer to work on the same problem in parallel rather than critique the answer. If the submit-time forward fails (transport glitch, reviewer pane gone), the turn isn't silently retried at Stop time — errors land in `~/.claude/kibitz-hook.log` and you can resend with `/tee` manually.

Claude itself still sees the `/mute` or `/tee` suffix in your prompt — Claude Code hooks can observe a prompt but can't rewrite it in place. Treat the trailing directive as benign noise; the model ignores it.

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
2. Copies `scripts/tmux-bridge`, `kibitz`, and the `hooks/kibitz_*.py` scripts (Claude hooks + `kibitz_codex_stop.py`) into `~/.local/bin/`.
3. Merges the Stop and UserPromptSubmit hook entries from `hooks/kibitz_hook.json` into `~/.claude/settings.json` (idempotent — won't duplicate on reinstall, preserves any other hooks you have).
4. Merges the codex Stop hook from `hooks/kibitz_codex_hooks.json` into `~/.codex/hooks.json` (same idempotent behavior), and sets `[features] codex_hooks = true` in `~/.codex/config.toml` so codex actually fires hooks. If `codex_hooks` is already set to `false` the installer warns and leaves it alone.
5. Adds `~/.local/bin/` to `PATH` in your shell rc (`.zshrc` / `.bashrc` / `.profile`) if it isn't already.
6. Warns if `codex` or `claude` binaries are missing (non-fatal).

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

# Forward the agent's own last reply verbatim to the counterpart pane:
#   codex reviewer pane → claude host  (header: [kibitz from:codex]), sent now
#   claude host pane    → codex reviewer (header: [kibitz from:claude]), sent
#                         when the current turn ends; what you see above the
#                         input is what goes out
# Running it twice sends twice — there's no dedupe.
kibitz relay

# Same, with your own note appended as a USER block:
kibitz relay "does the claim about X actually hold?"

# Remove the Stop hook entries and delete all installed scripts.
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
│   ├── kibitz_hook_stop.py                 # Claude Code Stop hook — forwards user/assistant exchange, stashes last_assistant_message for `kibitz relay`
│   ├── kibitz_hook_user_prompt_submit.py   # Claude Code UserPromptSubmit hook — implements `/tee`
│   ├── kibitz_hook_common.py               # shared helpers imported by both Claude hooks
│   ├── kibitz_hook.json                    # settings.json fragment `install` merges in
│   ├── kibitz_codex_stop.py                # Codex Stop hook — stashes last_assistant_message for `kibitz relay`
│   └── kibitz_codex_hooks.json             # ~/.codex/hooks.json fragment `install` merges in
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
├── kibitz_codex_stop.py
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

And to `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "$HOME/.local/bin/kibitz_codex_stop.py" }
        ]
      }
    ]
  }
}
```

Plus `[features] codex_hooks = true` in `~/.codex/config.toml` (codex won't fire hooks without it).

At runtime the codex Stop hook writes its relay payload `codex-<thread>.msg` under `$XDG_CACHE_HOME/kibitz/` (falls back to `~/.cache/kibitz/`), keyed on `CODEX_THREAD_ID` and overwritten atomically at the end of each codex turn. A claude host's `kibitz relay` writes nothing: it prints a `[kibitz] relay queued` line followed by your note, Claude Code records that output as the command's transcript entry whose parent links lead to whatever was visible above the input, and the Claude Stop hook that ends the turn follows those links to the reply it sends. It tries once; if the reviewer pane is gone or the send fails, it logs to `~/.claude/kibitz-hook.log` and drops it. Errors from the codex hook land in `$XDG_CACHE_HOME/kibitz/log`.

## Troubleshooting

- **`tmux-bridge not found`** — run `kibitz install`, or check that `~/.local/bin` is on `PATH`.
- **Hook not forwarding (Claude side)** — check `~/.claude/kibitz-hook.log`. The hook always exits 0 (so it can't block your session), and routes errors there.
- **`kibitz relay` says `no relay payload`** — the codex Stop hook hasn't written a stash for this thread yet. Check `$XDG_CACHE_HOME/kibitz/log` for codex Stop hook errors and confirm `[features] codex_hooks = true` in `~/.codex/config.toml` — without it codex won't fire hooks at all. A claude-host relay that never arrives means the Claude Stop hook didn't run or failed; check `~/.claude/kibitz-hook.log`, and note the hook only runs when Claude's turn ends.
- **`kibitz relay` says `CODEX_THREAD_ID is not set`** — only the codex-reviewer path requires it; the claude-host path keys on `$TMUX_PANE`. You're likely running relay from a codex-labeled pane where codex didn't inherit the env var (unusual — it's normally exported automatically).
- **`couldn't detect host agent`** — host detection is Linux-only (`/proc/$PPID/exe`). On macOS, or when running through an unusual wrapper, pass the agent explicitly: `kibitz start codex`.
- **Stale reviewer pane** — if `kibitz status` reports a pane that no longer exists, something killed the pane outside of kibitz. Run `kibitz stop` to clear the label, then `kibitz start`.

## Uninstall

```bash
kibitz uninstall
```

Removes the kibitz hook entries from `~/.claude/settings.json` and `~/.codex/hooks.json` (preserving any unrelated hooks you added), deletes `kibitz`, the hook scripts, and `tmux-bridge` from `~/.local/bin/`, and clears `$XDG_CACHE_HOME/kibitz/` (stashed codex payloads and hook log). Leaves `[features] codex_hooks = true` in `~/.codex/config.toml` alone — the flag is harmless with no hooks registered, and disabling it would silently break other hooks you may have added independently. Leaves `PATH` edits in your shell rc alone.
