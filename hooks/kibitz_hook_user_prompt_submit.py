#!/usr/bin/env python3
"""Kibitz prompt handling, shared by Claude Code and Codex.

An explicit trailing /tee sends the stripped prompt to the paired pane in
either direction. Ordinary Claude host prompts reserve their later review.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.
"""
import json
import sys
import kibitz_hook_common as common

from kibitz_notifications import notify, remember_prompt

from kibitz_hook_common import (
    REVIEWER_LABELS,
    current_pane_label,
    forward_prompt,
    log,
    parse_directive,
    is_reviewer_originated,
    is_task_notification,
    resolve_counterpart,
    resolve_reviewer,
    report_delivery_failure,
)


def handle_prompt(payload, agent="claude"):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    # Internal, non-persisted Codex threads share the terminal's environment.
    if agent == "codex" and payload.get("transcript_path", "") is None:
        return 0

    # Stop resolves task ownership from the transcript. Reserving the
    # wakeup's new prompt ID here would leave an unrelated review outstanding.
    if is_task_notification(prompt):
        return 0

    cleaned, directive = parse_directive(prompt)
    if agent == 'codex' and (directive != 'tee' or not cleaned or is_reviewer_originated(prompt)):
        return 0
    if not common.valid_source():
        return 0
    if directive == "tee" and cleaned and not is_reviewer_originated(prompt):
        counterpart = resolve_counterpart(agent)
        if not counterpart:
            report_delivery_failure("no live paired pane in this window")
            return 0
        pane_id, target_agent, to_reviewer = counterpart
        # The indicator's reservations describe host -> reviewer work. A
        # reverse /tee is ordinary work in both panes, not a reversed pairing.
        handoff = dict(payload, prompt_id=payload.get("prompt_id") or payload.get("turn_id"))
        message = f"[kibitz from:{agent}]\n\nUSER:\n{cleaned}"
        if to_reviewer and agent == "claude":
            remember_prompt(payload)

        def reserve():
            notify("expect", handoff, pane_id)
            notify("queue", handoff, pane_id, message)

        try:
            forward_prompt(pane_id, message, expected_agent=target_agent,
                           before_paste=reserve if to_reviewer else None)
        except Exception as e:
            if to_reviewer:
                notify(common.delivery_failure_action(e), handoff, pane_id)
            report_delivery_failure(e)
        return 0

    # Codex does not automatically forward its replies. Do not reserve a
    # review for a normal prompt or for incoming Kibitz messages.
    if agent != "claude":
        return 0
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
    return 0


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin json (user_prompt_submit): {e}")
        return 0
    return handle_prompt(payload)


if __name__ == "__main__":
    sys.exit(main())
