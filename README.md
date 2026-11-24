# kibitz

A sidecar reviewer agent for Claude Code and Codex. Splits your tmux window, runs the *other* agent in the new pane, and forwards each user/assistant exchange from the host to the reviewer via a Claude Code Stop hook. The reviewer sees the discussion in near real time; by convention it stays quiet and only replies when the user asks it to.

Named after the Yiddish *kibbitzer* — the spectator who can't resist offering an opinion.

## What it does

- `kibitz start` splits the current tmux window (vertical divider), launches the reviewer agent in the new pane, labels the pane via smux (`codex` or `claude-reviewer`).
- A Claude Code `Stop` hook extracts the latest user + assistant turn from the session transcript and forwards them through Kibitz’s guarded sender to the reviewer pane.
- The reviewer sends messages back two ways, both prepend a `[kibitz from:<agent>]` header so the host can tell them apart from real user input:
  - `kibitz send "<text>"` — send custom text.
  - `kibitz relay [text]` — forward the agent's own last assistant reply verbatim to its counterpart pane. With text, the reply goes out framed as a `CLAUDE:` (or `CODEX:`) block followed by a `USER:` block holding the text — your own note or question about that reply. From a claude host pane the relay is sent by the Claude `Stop` hook when the current turn ends (one attempt); it sends the reply that was shown above your input when you ran the command, so rewinding first is honored. Deliveries (relays and forwarded exchanges alike) check codex's rollout log first: if codex is mid-task the text is submitted with Tab, which codex queues for after the current task; `kibitz relay --force <text>` submits with Enter and interrupts. From a codex reviewer pane it forwards codex's last reply immediately, read from the `codex-<thread>.msg` stash the codex `Stop` hook writes under `$XDG_CACHE_HOME/kibitz/` keyed on `CODEX_THREAD_ID`. No dedupe — running `relay` twice on the same turn sends twice.
- `kibitz stop` / `kibitz restart` / `kibitz status` manage the pane lifecycle.
- `kibitz uninstall` tears everything down cleanly.

### Behavior conventions

- **Reviewer is read-only by default.** The sidecar is meant to watch and comment, not drive. It should only message the host back when the user explicitly asks it to relay something.
- **Host can tell who typed what.** Anything that arrives at the host with a `[kibitz from:<agent>]` header came from the reviewer (emitted by `kibitz send`). Anything without it is real user input.
- **Slash commands are not forwarded.** If the most recent user turn starts with `/` (like `/clear`), the Stop hook skips forwarding that exchange.
- **Recursion-safe.** Automatic review forwarding is disabled in reviewer panes. An explicit `/tee` works from either pane, but messages carrying a `[kibitz from:...]` header are never forwarded back.

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
- `/tee` — send your message immediately to the paired agent, from **either Claude or Codex**, in either the host or reviewer pane. For example, `Explain this error /tee` in Codex also sends `Explain this error` to its paired Claude pane. Only the user's prompt is shared; neither reply is sent back automatically. Pairing uses the pane IDs recorded by `kibitz start`, so other pairs and the currently focused pane do not affect delivery. If the submit-time forward fails, it is logged in `~/.claude/kibitz-hook.log` and is not retried at Stop time. Follow the delivery diagnostic before retrying.

The table's automatic reply forwarding applies to a Claude host. Ordinary prompts in Codex and in reviewer panes stay local; `/tee` explicitly shares a prompt from those panes. A bare `/tee` sends nothing. The paired panes must still be in the same tmux window.

Replies to an incoming `/tee` stay local even when Claude wraps the message as pasted content
or finishes its answer after background work. The next ordinary Claude prompt still forwards
normally. Use `kibitz relay` if you explicitly want to share the reply.

`/tee`, `kibitz send`, `kibitz relay`, and automatic reply forwarding all check that the destination
has a live foreground agent and a normal prompt before pasting, then check again before submitting.
The sending process must descend from the pane it claims; a daemon with another pane's environment
cannot route a message or replace that pane's delivery metadata. It refuses shell prompts, permission/question dialogs,
copy mode and unknown input screens. Claude's native session status is checked independently of
the indicator colour. Claude at a normal prompt with a background shell is allowed. Long input and
existing drafts are allowed; a draft may be submitted together with the forwarded message.
A brief cursor redraw is retried for up to three seconds within the same attempt. Permission
questions and changed agent identities stop delivery. The target is reread after reserving a review;
tmux rechecks its composer/footer text, cursor, and mode with the paste and submit commands.
If the destination is unavailable, Kibitz logs the reason and shows a tmux status message.
`kibitz status` includes outstanding delivery errors. The latest error also remains in the source pane's `@kibitz-delivery-error` option until a successful
send, and in `/tmp/kibitz-<uid>/delivery-errors.log`. Process checks use native macOS APIs or Linux
`/proc`, and delivery locks live in that private temporary directory, including inside a Codex tool sandbox. “Not sent”
means no paste occurred. “Text pasted, but no submit key sent” or “delivery unconfirmed” means you
should inspect the other pane before retrying, to avoid sending the same message twice. No delivery
is automatically retried. `--force` only selects Enter instead of Tab for a busy Codex; it never
bypasses these checks. Busy rollout records are checked against the live agent and tmux server.
These checks reduce terminal-input races; the application can still change state between a check
and receiving a keystroke. They do not provide atomic delivery into the agent.

Both agents still see the `/mute` or `/tee` suffix in your original prompt; Kibitz strips it only from the forwarded copy. Treat the trailing directive as benign noise.

### Completion sounds with tmux-agent-indicator

With tmux-agent-indicator installed, a window rings
once after Claude and its reviewer are ready, including reviews queued behind earlier work. `/mute`
needs no review; `/tee` waits for the review started when you submitted the prompt. Ctrl+C cancels the
interrupted review without announcing successful completion. If Codex returns a queued review to its
input box, tracking resumes when you submit it; discarding the draft does not block later alerts.
Replacing a prompt or explicitly stopping all of its background tasks cancels its undispatched review.
Submissions abandoned before they enter the transcript are cleared after a later prompt finishes,
provided the transcript checkpoint is intact and no background tasks remain.
If Claude delivers a task completion inside another turn, that turn keeps its review.
The older undispatched review is cleared only after all of its tasks explicitly finish or stop,
including a mixture of completed tasks, successful `TaskStop` calls, and Claude's own monitor-expiration
notices. Re-armed monitors and other unfinished work keep the review pending.
Queued prompts can be delivered under a new prompt ID or attached to an ongoing turn. Kibitz clears
their old reservations after delivery is recorded and the receiving turn finishes without background
work. It checks the exact prompt text, transcript checkpoint, pane and session; identical queued
prompts require distinct delivery records. A queue entry by itself does not count as delivery.
Other background work remains pending until it finishes; review reservations have no time limit.

The integration uses Claude prompt IDs or Codex turn IDs, and the reviewer's turn IDs.
Merged `/tee` prompts and drafts recognize both `[kibitz from:claude]` and `[kibitz from:codex]` headers. Delivery failures are logged in
`~/.claude/kibitz-hook.log`; the indicator also exposes `@agent-indicator-notification-error` on the
window. A refusal proven to have pasted nothing retires that attempt, so it cannot hold the completion
bell or steal a later retry's receipt. A retry of the same message replaces older attempts known to have sent no submit key.
Unconfirmed submissions remain tracked;
a receiver hook can confirm them. If you discard such a message, restart the pair to clear its
unconfirmed review. Kibitz works without the indicator installed.

## Requirements

- macOS or Linux
- tmux 3.7+
- Python 3.10+ (`python3` on PATH)
- `curl` or `wget` (for install)
- One of `codex` or `claude` CLIs on PATH for whichever agents you want to run

Codex reviewers run with `--no-daemon` so their hooks stay in the tmux pane's process tree.
Use a Codex version that supports this option.

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
4. Merges the Codex SessionStart, UserPromptSubmit and Stop hooks from `hooks/kibitz_codex_hooks.json` into `~/.codex/hooks.json` (same idempotent behavior), and sets `[features] codex_hooks = true` in `~/.codex/config.toml` so Codex actually fires hooks. If `codex_hooks` is already set to `false` the installer warns and leaves it alone.
5. Adds `~/.local/bin/` to `PATH` in your shell rc (`.zshrc` / `.bashrc` / `.profile`) if it isn't already.
6. Warns if `codex` or `claude` binaries are missing (non-fatal).

`tmux-bridge` is vendored from [`ShawnPana/smux`](https://github.com/ShawnPana/smux) under `scripts/`.

After install, open a new shell (or `source` your rc) so `PATH` picks up the new entries.

After updating an existing installation, restart/resume Codex so it loads the
new UserPromptSubmit hook. Approve the updated hooks in `/hooks` if Codex asks.
Claude's existing prompt hook uses the updated script on its next invocation.

## Usage

All commands work whether you invoke `./kibitz` from the repo or `kibitz` from PATH after install.

```bash
# Start a sidecar. Auto-detects the host agent and launches the other one.
# An explicitly named host reuses a matching reviewer in the host's directory.
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

When the host has an explicit name, `kibitz start` first looks for a saved
conversation belonging to the reviewer agent with the **exact same name and
recorded working directory**. One match is resumed by ID with the usual Kibitz
role and pane pairing. With no match, it starts a fresh conversation **with the
host's name**, so the next start can resume it without a manual rename. Multiple
matches, unavailable metadata, or no explicit host name start a fresh, unnamed
conversation. Names in other directories
are ignored; symlink paths resolving to the same directory count as the same
location. A tool command's temporary working directory does not change the match:
Kibitz uses the host conversation's recorded directory for lookup and launch.
If the host directory cannot be identified or no longer exists, it starts fresh
in the invoking shell's current directory. `restart` applies the same rule.

For Claude hosts, only live session records marked `nameSource: user` qualify;
derived folder names are ignored. For Codex hosts, Kibitz uses the saved name,
never the first-message preview. Codex does not record whether a saved name came
from `/rename` or another client. Claude reviewer matches use saved custom titles,
not AI-generated titles. Names are inherited only at startup; renaming a running
host does not rename its reviewer.

Lookup is local and read-only. Kibitz checks for a saved conversation before
launching, so a missing name starts fresh without opening a resume picker. It
does not fall back to a new conversation for unrelated errors after launch.
`CLAUDE_CONFIG_DIR` is preserved exactly when explicitly set and remains unset
otherwise, so starting a reviewer does not select a different Claude login.

Claude receives its new name through the native `--name` option. For Codex,
Kibitz briefly runs a local app-server to create an empty thread, set and verify
its name, and then open that thread in the reviewer TUI. This uses the
[official OpenAI app-server protocol](https://learn.chatgpt.com/docs/app-server).
No model turn or terminal keystrokes are used to name it. The temporary server
has hooks disabled; the reviewer runs its usual hooks on resume. This adds a
short startup step only for newly named Codex reviewers. A failed naming step
reports an error instead of silently opening a second, unnamed conversation.

### Talking to the reviewer

Kibitz's guarded sender handles forwarding. `tmux-bridge` remains available for direct terminal
control; its raw `type` and `keys` commands do not apply Kibitz's delivery checks. Labels are `codex` or `claude-reviewer` depending on which agent is running.

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
│   ├── kibitz_hook_user_prompt_submit.py   # shared prompt handler — implements `/tee` in either direction
│   ├── kibitz_hook_common.py               # shared pairing, directive and transport helpers
│   ├── kibitz_hook.json                    # settings.json fragment `install` merges in
│   ├── kibitz_codex_stop.py                # Codex hooks — prompt forwarding, rollout mapping and relay stash
│   └── kibitz_codex_hooks.json             # ~/.codex/hooks.json fragment `install` merges in
├── scripts/
│   ├── kibitz_reviewer_launch  # fresh/resumed reviewer role setup
│   ├── kibitz_session.py       # explicit host name and same-directory session lookup
│   ├── kibitz_codex_start.py   # create and name an empty Codex thread before opening its TUI
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
├── kibitz_reviewer_launch
├── kibitz_session.py
├── kibitz_codex_start.py
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
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "$HOME/.local/bin/kibitz_codex_stop.py" }
        ]
      }
    ],
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

## Conversation recovery

For tmux-agent-indicator recovery, update both packages before saving a Kibitz pair.
The saved host/reviewer link is rebuilt when tmux restarts, and the reviewer reopens
with its existing conversation and Kibitz role settings. The launcher accepts
`kibitz_reviewer_launch resume codex SESSION` or `kibitz_reviewer_launch resume claude SESSION`.

## Troubleshooting

- **`/tee` from Codex does nothing after an upgrade** — exit and resume the running Codex session,
  then open `/hooks` and trust the newly installed Kibitz `UserPromptSubmit` hook. The installer
  prints this notice when it adds that hook. Also check `~/.claude/kibitz-hook.log` for an unresolved
  pair or a source-pane validation failure.
- **Delivery requires tmux 3.7** — upgrade the tmux server as well as the binary. A server started
  with an older version must be restarted before the guarded sender can use it.

- **`tmux-bridge not found`** — run `kibitz install`, or check that `~/.local/bin` is on `PATH`.
- **Hook not forwarding (Claude side)** — check `~/.claude/kibitz-hook.log`. The hook always exits 0 (so it can't block your session), and routes errors there.
- **`kibitz relay` says `no relay payload`** — the codex Stop hook hasn't written a stash for this thread yet. Check `$XDG_CACHE_HOME/kibitz/log` for codex Stop hook errors and confirm `[features] codex_hooks = true` in `~/.codex/config.toml` — without it codex won't fire hooks at all. A claude-host relay that never arrives means the Claude Stop hook didn't run or failed; check `~/.claude/kibitz-hook.log`, and note the hook only runs when Claude's turn ends.
- **`kibitz relay` says `CODEX_THREAD_ID is not set`** — only the codex-reviewer path requires it; the claude-host path keys on `$TMUX_PANE`. You're likely running relay from a codex-labeled pane where codex didn't inherit the env var (unusual — it's normally exported automatically).
- **`couldn't detect host agent`** — Kibitz uses `CLAUDECODE` or `CODEX_THREAD_ID` when available, then process ancestry on Linux. When running through a shell or wrapper without those markers, pass the agent explicitly: `kibitz start codex`.
- **Stale reviewer pane** — if `kibitz status` reports a pane that no longer exists, something killed the pane outside of kibitz. Run `kibitz stop` to clear the label, then `kibitz start`.

## Uninstall

```bash
kibitz uninstall
```

Removes the kibitz hook entries from `~/.claude/settings.json` and `~/.codex/hooks.json` (preserving any unrelated hooks you added), deletes `kibitz`, the hook scripts, and `tmux-bridge` from `~/.local/bin/`, and clears `$XDG_CACHE_HOME/kibitz/` (stashed codex payloads and hook log). Leaves `[features] codex_hooks = true` in `~/.codex/config.toml` alone — the flag is harmless with no hooks registered, and disabling it would silently break other hooks you may have added independently. Leaves `PATH` edits in your shell rc alone.
