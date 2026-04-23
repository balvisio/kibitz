#!/usr/bin/env python3
"""kibitz_hook_stop — Claude Code Stop hook that forwards the latest exchange
to a kibitz reviewer pane (codex or claude-reviewer) via tmux-bridge, and also
stashes the host's last_assistant_message for manual `kibitz relay`.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.

Directives on the user's message:
  - "/mute" (trailing) — this exchange is not forwarded.
  - "/tee"  (trailing) — the user text was already forwarded at submit time by
    kibitz_hook_user_prompt_submit.py; the reply is intentionally not sent.

Replies to reviewer-originated messages (those carrying a '[kibitz from:...]'
header from `kibitz send`) are not forwarded back, to prevent host/reviewer
ping-pong loops.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

from kibitz_hook_common import (
    REVIEWER_LABELS,
    LAST_FORWARD_PATH,
    current_pane_label,
    extract_text,
    forward,
    is_reviewer_originated,
    is_skippable_user_text,
    log,
    parse_directive,
    resolve_reviewer,
)

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kibitz"


def stash_host_turn(pane_id, payload):
    """Atomically write last_assistant_message to claude-<pane>.msg so a
    manual `kibitz relay` from the same host pane can forward it to the
    reviewer. Keyed on $TMUX_PANE because Claude Code — unlike codex — does
    not export a session/thread ID into child shells, so pane_id is the only
    stable identifier a spawned shell can observe.

    Best-effort: any failure is logged and swallowed so the stash cannot
    break the Stop hook's primary job (forwarding exchanges)."""
    raw = payload.get("last_assistant_message") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"stash: cache dir create failed: {e}")
        return

    key = pane_id.lstrip("%")
    msg_path = CACHE_DIR / f"claude-{key}.msg"
    tmp_path = msg_path.with_suffix(".msg.tmp")
    session_id = payload.get("session_id", "") if isinstance(payload, dict) else ""
    body = {"turn_id": session_id, "session_id": session_id, "message": raw}
    try:
        with tmp_path.open("w") as f:
            json.dump(body, f)
        tmp_path.replace(msg_path)
    except Exception as e:
        log(f"stash: write failed for {msg_path}: {e}")


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

    pane_label = current_pane_label()
    my_pane = os.environ.get("TMUX_PANE", "")

    # Stash the host's last_assistant_message for manual `kibitz relay`. Runs
    # before any transcript/forward preconditions so relay still works when
    # automatic forwarding is skipped (muted, deduped, reviewer pane gone).
    # Scoped to non-reviewer panes: stashing a reviewer's own reply isn't
    # wired into the relay flow and would just litter the cache dir.
    if my_pane and pane_label not in REVIEWER_LABELS:
        stash_host_turn(my_pane, payload)

    transcript = payload.get("transcript_path")
    if not transcript:
        return 0
    tpath = Path(transcript)
    if not tpath.is_file():
        return 0

    if pane_label in REVIEWER_LABELS:
        return 0

    reviewer = resolve_reviewer()
    if not reviewer:
        return 0
    pane_id, _reviewer_label = reviewer

    user_text_raw = latest_user_prompt(tpath)
    if not user_text_raw:
        return 0

    # Don't relay replies to reviewer-originated messages back to the reviewer
    # — otherwise "[kibitz from:codex] hi" -> claude replies "hi" -> forwarded
    # to codex -> codex replies -> loop.
    if is_reviewer_originated(user_text_raw):
        return 0

    user_text, directive = parse_directive(user_text_raw)
    # /mute: drop this exchange. /tee: user text was already forwarded at
    # submit time; skip the Stop-time forward so the reviewer never sees the
    # reply for it. Option B falls out of this too: a bare /mute or /tee leaves
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
