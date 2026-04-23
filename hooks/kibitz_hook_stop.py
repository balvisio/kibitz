#!/usr/bin/env python3
"""kibitz_hook_stop — Claude Code Stop hook that forwards the latest exchange
to a kibitz reviewer pane (codex or claude-reviewer) via tmux-bridge.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.

Directives on the user's message:
  - "/mute" (trailing) — this exchange is not forwarded.
  - "/dup"  (trailing) — the user text was already forwarded at submit time by
    kibitz_hook_user_prompt_submit.py; the reply is intentionally not sent.
"""
import hashlib
import json
import sys
from pathlib import Path

from kibitz_hook_common import (
    REVIEWER_LABELS,
    LAST_FORWARD_PATH,
    current_pane_label,
    extract_text,
    forward,
    is_skippable_user_text,
    log,
    parse_directive,
    resolve_reviewer,
)


def latest_user_prompt(transcript_path):
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
        if not text or is_skippable_user_text(text):
            continue
        return text

    return None


def main():
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

    user_text_raw = latest_user_prompt(tpath)
    if not user_text_raw:
        return 0

    user_text, directive = parse_directive(user_text_raw)
    # /mute: drop this exchange. /dup: user text was already forwarded at
    # submit time; skip the Stop-time forward so the reviewer never sees the
    # reply for it. Option B falls out of this too: a bare /mute or /dup leaves
    # user_text empty with a directive set, and we return here.
    if directive:
        return 0

    raw = payload.get("last_assistant_message") if isinstance(payload, dict) else None
    assistant_text = raw.strip() if isinstance(raw, str) else ""
    if not assistant_text:
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
