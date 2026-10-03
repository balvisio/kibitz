#!/usr/bin/env python3
"""kibitz_hook_stop — Claude Code Stop hook that forwards the latest exchange
to a kibitz reviewer pane (codex or claude-reviewer) via tmux-bridge, and
delivers relays queued by `kibitz relay` from the host pane.

Invoked with the hook payload on stdin. Exits 0 unconditionally so hook
failures never block the session; errors go to ~/.claude/kibitz-hook.log.

Directives on the user's message:
  - "/mute" (trailing) — this exchange is not forwarded.
  - "/tee"  (trailing) — the user text was already forwarded at submit time by
    kibitz_hook_user_prompt_submit.py; the reply is intentionally not sent.

Replies to reviewer-originated messages (those carrying a '[kibitz from:...]'
header from `kibitz send`) are not forwarded back, to prevent host/reviewer
ping-pong loops.

Claude Code also fires Stop when a turn merely pauses for background work
(Monitor, background Bash, async Agent, Workflow) and later resumes from a
<task-notification> user entry. Those interim replies are not forwarded: the
exchange goes out once the work launched for the user's prompt has finished,
attributed to that prompt rather than to the notification.

`kibitz relay [note]` from the host pane sends nothing itself: it prints a
marker line and the note. Claude Code records the command and its output
after it exits, with a parent link to whatever was visible above the input at
that moment — the only trace a conversation rewind leaves. At the Stop that
ends the turn started by that command, this hook finds the recorded output,
follows the links to the last assistant reply and sends it. One attempt; any
failure is logged and dropped.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

from kibitz_hook_common import (
    REVIEWER_LABELS,
    LAST_FORWARD_PATH,
    current_pane_label,
    extract_text,
    forward,
    is_command_text,
    is_reviewer_originated,
    is_skippable_user_text,
    log,
    parse_directive,
    resolve_reviewer,
)

# toolUseResult keys under which Claude Code records the id of a task it put
# in the background (Monitor and Workflow, Bash run_in_background, Agent). The
# same id shows up in background_tasks[].id of the Stop payload and in the
# <task-id> of the notification that later resumes the session.
_TASK_ID_KEYS = ("taskId", "backgroundTaskId", "agentId")


def load_entries(transcript_path):
    entries = []
    with transcript_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def user_text(entry):
    """Text of a user entry; "" for tool results and non-user entries."""
    if entry.get("type") != "user":
        return ""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    return extract_text(msg.get("content"))


def is_task_notification(text):
    return text.lstrip().startswith("<task-notification>")


def launched_task_ids(entry):
    result = entry.get("toolUseResult")
    if not isinstance(result, dict):
        return set()
    return {result[key] for key in _TASK_ID_KEYS if isinstance(result.get(key), str)}


RELAY_LINE = "[kibitz] relay queued - goes to the reviewer when this turn ends"
RELAY_LINE_FORCE = "[kibitz] relay queued (force) - goes to the reviewer when this turn ends, even mid-task"
# The launcher's instruction to the host agent; Claude Code records stderr
# together with stdout, so it must be dropped from the note.
RELAY_HINT_PREFIX = "[kibitz] host agent:"


def command_output(entry):
    """Where a kibitz command's output lands in the transcript: a
    <bash-stdout> record for `! kibitz ...`, or the Bash tool result when
    Claude ran the command itself. Anything else — in particular a typed
    prompt quoting the marker — is not command output."""
    text = user_text(entry)
    if text:
        return text if text.lstrip().startswith("<bash-stdout>") else ""
    result = entry.get("toolUseResult")
    if isinstance(result, dict) and isinstance(result.get("stdout"), str):
        return result["stdout"]
    return ""


def relay_note_lines(entry):
    """(note lines, forced) of a `kibitz relay` run, when this entry records
    its output: the launcher prints exactly RELAY_LINE or RELAY_LINE_FORCE
    first, then the note. None for any other output, including text that
    merely contains the marker (a grep of this file, a cat of the launcher)."""
    output = command_output(entry)
    if not output:
        return None
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    m = re.search(r"<bash-stdout>(.*?)</bash-stdout>", clean, re.S)
    lines = (m.group(1) if m else clean).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or lines[0].strip() not in (RELAY_LINE, RELAY_LINE_FORCE):
        return None
    note_lines = [line for line in lines[1:] if not line.strip().startswith(RELAY_HINT_PREFIX)]
    return note_lines, lines[0].strip() == RELAY_LINE_FORCE


def queued_relay(entries):
    """(anchor, note, forced) for a `kibitz relay` run during the turn being
    stopped, recognised by the line it printed; None when this turn has none. The
    scan ends at whatever started the turn — a prompt, a notification or a
    shell command's record — so an earlier relay is never picked up again."""
    for entry in reversed(entries):
        found = relay_note_lines(entry)
        if found is not None:
            note_lines, forced = found
            return entry, "\n".join(note_lines).strip(), forced
        text = user_text(entry)
        if not text:
            continue
        if is_task_notification(text) or is_command_text(text) or not is_skippable_user_text(text):
            return None
    return None


def visible_reply(entries, anchor):
    """Last assistant reply above the point where the relay command ran,
    following parentUuid links. From a <bash-stdout> record the chain leads
    straight there; from a Bash tool result it first crosses Claude's own
    turn, so walk past the prompt that started that turn."""
    by_uuid = {e.get("uuid"): e for e in entries if e.get("uuid")}
    skip_to_prompt = not user_text(anchor)
    entry = by_uuid.get(anchor.get("parentUuid"))
    while entry is not None:
        if skip_to_prompt:
            text = user_text(entry)
            if text and not is_skippable_user_text(text) and not is_task_notification(text):
                skip_to_prompt = False
        elif entry.get("type") == "assistant":
            text = extract_text((entry.get("message") or {}).get("content"))
            if text:
                return text
        entry = by_uuid.get(entry.get("parentUuid"))
    return ""


def send_queued_relay(pane_id, entries, anchor, note, force):
    reply = visible_reply(entries, anchor)
    if not reply:
        log("relay: no assistant reply found above the relay command; dropped")
        return
    body = f"CLAUDE:\n{reply}\n\nUSER:\n{note}" if note else reply
    try:
        forward(pane_id, f"[kibitz from:claude]\n\n{body}", force=force)
    except Exception as e:
        log(f"relay: forward failed; dropped: {e}")


def current_exchange(entries):
    """Return (prompt_text, tasks): the human prompt that owns the turn being
    stopped and the ids of every background task launched on its behalf.

    A turn is started by a typed prompt, a <task-notification> or a `! shell`
    command. A notification-started turn belongs to whichever prompt owns the
    task that produced it, so ownership follows notification ancestry through
    any chain of launches, and tasks launched during a turn belong to that
    turn's prompt. Turns started by a shell command, or by a notification
    that cannot be traced to a launch in this transcript, own nothing and are
    never forwarded. Slash-command records never start a reply and may land
    inside an ongoing turn, so they leave ownership untouched. User entries
    are flushed long before the Stop hook fires, so
    reading the transcript here is safe — unlike the final assistant text
    block, which is often still buffered at hook time."""
    owner = None
    task_owner = {}
    tasks = {}
    for i, entry in enumerate(entries):
        text = user_text(entry)
        if not text:
            if owner is not None:
                for task_id in launched_task_ids(entry):
                    task_owner[task_id] = owner
                    tasks.setdefault(owner, set()).add(task_id)
        elif is_task_notification(text):
            ids = re.findall(r"<task-id>([^<]+)</task-id>", text)
            known = [task_owner[task_id] for task_id in ids if task_id in task_owner]
            owner = known[0] if known else None
        elif is_command_text(text):
            owner = None
        elif not is_skippable_user_text(text):
            owner = i
    if owner is None:
        return None, set()
    return user_text(entries[owner]), tasks.get(owner, set())


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin json: {e}")
        return 0

    pane_label = current_pane_label()

    transcript = payload.get("transcript_path")
    entries = load_entries(Path(transcript)) if transcript and Path(transcript).is_file() else None
    if entries is None:
        return 0

    if pane_label in REVIEWER_LABELS:
        return 0

    reviewer = resolve_reviewer()
    relay = queued_relay(entries)
    if relay and not reviewer:
        log("relay: no reviewer pane in this window; dropped")
    if not reviewer:
        return 0
    pane_id, _reviewer_label = reviewer

    if relay:
        send_queued_relay(pane_id, entries, *relay)

    user_text_raw, launched = current_exchange(entries)
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

    # Stop also fires when the turn only pauses for background work that will
    # wake the session again; background_tasks lists what is still in flight.
    # If any of it belongs to this prompt, the reply is interim status.
    pending = set()
    for task in payload.get("background_tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("id"), str):
            pending.add(task["id"])
    if launched & pending:
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
