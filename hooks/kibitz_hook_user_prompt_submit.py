#!/usr/bin/env python3
"""kibitz_hook_user_prompt_submit — Claude Code UserPromptSubmit hook: when
the user's prompt ends with the "/tee" directive, immediately forward the
stripped prompt to the kibitz reviewer pane. The Stop hook drops /tee-tagged
exchanges on the reply side, so the reviewer sees only the user's message.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.
"""
import json
import sys

from kibitz_notifications import notify, remember_prompt

from kibitz_hook_common import (
    REVIEWER_LABELS,
    current_pane_label,
    forward,
    log,
    parse_directive,
    is_reviewer_originated,
    is_task_notification,
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

    # Stop resolves task ownership from the transcript. Reserving the
    # wakeup's new prompt ID here would leave an unrelated review outstanding.
    if is_task_notification(prompt):
        return 0

    cleaned, directive = parse_directive(prompt)
    if current_pane_label() in REVIEWER_LABELS:
        return 0

    reviewer = resolve_reviewer()
    if not reviewer:
        return 0
    pane_id, _reviewer_label = reviewer
    remember_prompt(payload)
    notify("expect", payload, pane_id)
    if directive == "mute" or is_reviewer_originated(prompt) or not cleaned:
        notify("skip", payload, pane_id)
        return 0
    if directive != "tee":
        return 0

    message = f"[kibitz from:claude]\n\nUSER:\n{cleaned}"
    notify("queue", payload, pane_id, message)
    try:
        forward(pane_id, message)
    except Exception as e:
        notify("fail", payload, pane_id)
        log(f"forward failed (user_prompt_submit): {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
