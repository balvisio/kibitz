#!/usr/bin/env python3
"""kibitz_hook_user_prompt_submit — Claude Code UserPromptSubmit hook: when
the user's prompt ends with the "/dup" directive, immediately forward the
stripped prompt to the kibitz reviewer pane. The Stop hook drops /dup-tagged
exchanges on the reply side, so the reviewer sees only the user's message.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.
"""
import json
import sys

from kibitz_hook_common import (
    REVIEWER_LABELS,
    current_pane_label,
    forward,
    log,
    parse_directive,
    resolve_reviewer,
)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin json (user_prompt_submit): {e}")
        return 0

    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    cleaned, directive = parse_directive(prompt)
    if directive != "dup":
        return 0
    # Option B: bare "/dup" is a no-op, not a forward-empty-body.
    if not cleaned:
        return 0

    if current_pane_label() in REVIEWER_LABELS:
        return 0

    reviewer = resolve_reviewer()
    if not reviewer:
        return 0
    pane_id, _reviewer_label = reviewer

    message = f"[kibitz from:claude]\n\nUSER:\n{cleaned}"
    try:
        forward(pane_id, message)
    except Exception as e:
        log(f"forward failed (user_prompt_submit): {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
