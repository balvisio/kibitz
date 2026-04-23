#!/usr/bin/env python3
"""kibitz_hook_stop — Claude Code Stop hook that forwards the latest exchange
to a kibitz reviewer pane (codex or claude-reviewer) via tmux-bridge.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

REVIEWER_LABELS = ("codex", "claude-reviewer")
LOG_PATH = Path.home() / ".claude" / "kibitz-hook.log"
LAST_FORWARD_PATH = Path.home() / ".claude" / "kibitz-last.txt"

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


def log(msg: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def extract_text(content) -> str:
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


def _is_skippable_user_text(text: str) -> bool:
    s = text.lstrip()
    if not s:
        return True
    if s.startswith("/"):
        return True
    return s.startswith(_SKIP_USER_PREFIXES)


def latest_user_prompt(transcript_path: Path) -> Optional[str]:
    """Scan the transcript backward for the most recent user entry that looks
    like a real prompt (not a tool_result, bash-input/output, command caveat,
    or slash command). User entries are flushed long before the Stop hook
    fires, so a plain backward scan is safe — unlike the final assistant text
    block, which is often still buffered at hook time."""
    entries = []
    with transcript_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "user":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        text = extract_text(msg.get("content"))
        if not text or _is_skippable_user_text(text):
            continue
        return text

    return None


def current_pane_label() -> str:
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#{@name}"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def resolve_reviewer() -> Optional[tuple[str, str]]:
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


def forward(target: str, payload: str) -> None:
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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin json: {e}")
        return 0

    transcript = payload.get("transcript_path")
    if not transcript:
        return 0
    tpath = Path(transcript)
    if not tpath.is_file():
        return 0

    if current_pane_label() in REVIEWER_LABELS:
        return 0

    reviewer = resolve_reviewer()
    if not reviewer:
        return 0
    pane_id, _reviewer_label = reviewer

    raw = payload.get("last_assistant_message") if isinstance(payload, dict) else None
    assistant_text = raw.strip() if isinstance(raw, str) else ""
    if not assistant_text:
        return 0

    user_text = latest_user_prompt(tpath)
    if not user_text:
        return 0

    session_id = payload.get("session_id", "") if isinstance(payload, dict) else ""
    fingerprint = hashlib.sha256(
        f"{session_id}\n{user_text}\n{assistant_text}".encode("utf-8")
    ).hexdigest()
    try:
        last_fp = LAST_FORWARD_PATH.read_text().strip()
    except FileNotFoundError:
        last_fp = ""
    except Exception:
        last_fp = ""
    if fingerprint == last_fp:
        return 0

    message = (
        "[kibitz from:claude]\n\n"
        f"USER:\n{user_text}\n\n"
        f"CLAUDE:\n{assistant_text}"
    )

    try:
        forward(pane_id, message)
        try:
            LAST_FORWARD_PATH.write_text(fingerprint)
        except Exception:
            pass
    except Exception as e:
        log(f"forward failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
