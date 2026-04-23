"""Shared helpers for kibitz Claude Code hooks.

Imported by kibitz_hook_stop.py and kibitz_hook_user_prompt_submit.py. Lives
alongside them in ~/.local/bin/ after install; Python's default sys.path[0]
(the invoked script's directory) makes `import kibitz_hook_common` resolve.
"""
import json
import os
import subprocess
from pathlib import Path

REVIEWER_LABELS = ("codex", "claude-reviewer")
LOG_PATH = Path.home() / ".claude" / "kibitz-hook.log"
LAST_FORWARD_PATH = Path.home() / ".claude" / "kibitz-last.txt"

# Case-sensitive. /MUTE, /Tee, etc. fall through as normal text — keeping the
# surface area narrow avoids accidentally swallowing content that merely
# resembles a directive.
KIBITZ_DIRECTIVES = ("/mute", "/tee")

# User-entry content that isn't a real prompt (Claude Code local-command
# artifacts, tool errors, slash commands). Any user entry whose stripped text
# starts with one of these — or a leading '/' — is skipped when picking the
# "latest user prompt" to forward.
_SKIP_USER_PREFIXES = (
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-caveat>",
    "<command-name>",
    "<tool_use_error>",
)


def log(msg):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def extract_text(content):
    """Content may be a string or a list of content blocks; return joined text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                s = block.strip()
                if s:
                    parts.append(s)
            elif isinstance(block, dict) and block.get("type") == "text":
                s = (block.get("text") or "").strip()
                if s:
                    parts.append(s)
        return "\n".join(parts).strip()
    return ""


def is_skippable_user_text(text):
    """Return True for user entries that aren't real prompts (tool results,
    bash wrappers, Claude Code slash commands). A bare /mute or /tee is *not*
    skipped — parse_directive in the caller handles those."""
    s = text.lstrip()
    if not s:
        return True
    if s.startswith("/"):
        if s.rstrip() in KIBITZ_DIRECTIVES:
            return False
        return True
    return s.startswith(_SKIP_USER_PREFIXES)


def is_reviewer_originated(text):
    """Return True if the user message was relayed from the reviewer via
    `kibitz send` — those carry a '[kibitz from:<agent>]' header. Replies to
    such messages should not be forwarded back: the reviewer already saw its
    own outbound text, and a round-trip produces a ping-pong loop with no new
    information."""
    return text.lstrip().startswith("[kibitz from:")


def parse_directive(text):
    """Return (cleaned_text, directive) where directive is "mute", "tee", or "".

    Directives must appear at the very end of the message, preceded by
    whitespace or be the entire message — so `path/to/mute` or `/muted` don't
    trigger. Match is case-sensitive."""
    stripped = text.rstrip()
    for token in KIBITZ_DIRECTIVES:
        if stripped.endswith(token):
            prefix = stripped[: -len(token)]
            if not prefix or prefix[-1].isspace():
                return prefix.rstrip(), token[1:]
    return text, ""


def current_pane_label():
    # Target $TMUX_PANE explicitly: `display-message -p` without `-t` returns
    # the *focused* pane's @name, so when the reviewer pane has focus (e.g.
    # right after `kibitz send`), the host's hook would see the reviewer label
    # and bail out thinking it was running inside the reviewer.
    pane = os.environ.get("TMUX_PANE", "")
    cmd = ["tmux", "display-message", "-p"]
    if pane:
        cmd += ["-t", pane]
    cmd += ["#{@name}"]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def resolve_reviewer():
    """Find a reviewer pane in the *current tmux window* only.

    Returning the pane_id (not just the label) lets us route via tmux-bridge
    unambiguously — `tmux-bridge resolve <label>` is server-wide, so with
    multiple `claude + kibitz` setups across windows it would pick whichever
    reviewer it happened to match first and cross-wire the exchanges.

    Returns (pane_id, label) or None.
    """
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-F", "#{pane_id} #{@name}"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except Exception:
        return None
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pane_id, label = parts[0], parts[1].strip()
        if label in REVIEWER_LABELS:
            return pane_id, label
    return None


def forward(target, payload):
    """`target` can be a tmux pane_id (e.g. `%5`) or a @name label — tmux-bridge
    accepts either. We pass pane_id so routing stays pinned to the reviewer in
    *this* window, regardless of other reviewers elsewhere on the server."""
    subprocess.run(
        ["tmux-bridge", "read", target, "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=5,
    )
    subprocess.run(
        ["tmux-bridge", "type", target, payload],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=10,
    )
    # tmux-bridge's `type` clears the read-guard, so re-arm it before `keys`.
    subprocess.run(
        ["tmux-bridge", "read", target, "1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=5,
    )
    subprocess.run(
        ["tmux-bridge", "keys", target, "Enter"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=5,
    )
