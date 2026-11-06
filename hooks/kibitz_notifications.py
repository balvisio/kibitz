"""Optional completion-notification bridge to tmux-agent-indicator.

Register a message digest before sending it. The indicator matches the
reviewer's UserPromptSubmit and Stop turn IDs; keystrokes are not receipts.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from kibitz_hook_common import CACHE_DIR, extract_text, is_skippable_user_text, is_task_notification, log


def notify(action, payload, reviewer=None, message=None, prompt_ids=None):
    session, prompt = payload.get("session_id"), payload.get("prompt_id")
    host = os.environ.get("TMUX_PANE", "")
    if not session or not prompt or not host or not os.environ.get("TMUX"):
        return
    plugin = Path(os.environ.get("TMUX_AGENT_INDICATOR_DIR") or
                  Path.home() / ".tmux/plugins/tmux-agent-indicator")
    script = plugin / "scripts/agent-notify.sh"
    if not script.is_file():
        return
    try:
        pid = subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", host, "#{pane_pid}"],
            text=True, timeout=5).strip()
        data = dict(action=action, host=host, host_pid=pid,
                    session_id=session, prompt_id=prompt)
        if reviewer:
            data["reviewer"] = reviewer
        if prompt_ids is not None:
            data["prompt_ids"] = prompt_ids
        if message is not None:
            data["hash"] = hashlib.sha256(message.strip().encode()).hexdigest()
        subprocess.run(["bash", str(script)], input=json.dumps(data), text=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError) as error:
        log(f"notification handoff {action} failed: {type(error).__name__}")


def prompt_path(payload):
    session, prompt = payload.get("session_id"), payload.get("prompt_id")
    if not session or not prompt:
        return None
    key = hashlib.sha256((session + "\0" + prompt).encode()).hexdigest()
    return CACHE_DIR / ("prompt-" + key + ".json")


def prompt_owner():
    return [os.environ.get("TMUX", "").rsplit(",", 1)[0], os.environ.get("TMUX_PANE", "")]


def remember_prompt(payload):
    path = prompt_path(payload)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"prompt": payload.get("prompt", ""),
                       "session_id": payload.get("session_id"),
                       "prompt_id": payload.get("prompt_id"),
                       "owner": prompt_owner(),
                       "checkpoint": transcript_checkpoint(payload)}, stream)
        temporary.replace(path)
    except OSError as error:
        log(f"prompt capture failed: {type(error).__name__}")


def submission_text(entry):
    """A real user submission, excluding task wakeups and compact summaries."""
    if (entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain")
            or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly")):
        return ""
    text = extract_text((entry.get("message") or {}).get("content")).strip()
    if is_task_notification(text) or is_skippable_user_text(text):
        return ""
    return text


def transcript_checkpoint(payload):
    """Remember an existing record before an unrecorded submission starts."""
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    anchor = None
    try:
        with Path(transcript).open() as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    return None
                if not isinstance(entry, dict):
                    return None
                # A submission queued while Claude is working may initially
                # reuse the running turn's ID, including a task wakeup's ID.
                if (entry.get("promptId") == payload.get("prompt_id")
                        and submission_text(entry) == (payload.get("prompt") or "").strip()
                        and submission_text(entry)):
                    return None
                if entry.get("uuid"):
                    anchor = entry["uuid"]
        if anchor:
            return {"path": str(Path(transcript).resolve()), "anchor": anchor}
    except OSError:
        pass
    return None


def unrecorded_prompts(payload, entries):
    """Find submissions superseded before they reached an intact transcript.

    A later human prompt must have reached Stop and been written after the
    saved checkpoint. Missing history or outstanding background work is not
    evidence of abandonment.
    """
    if payload.get("background_tasks") != [] or not payload.get("transcript_path"):
        return []
    current = payload.get("prompt_id")
    users = {e.get("promptId") for e in entries if submission_text(e)}
    positions = {e.get("uuid"): i for i, e in enumerate(entries) if e.get("uuid")}
    current_index = next((i for i, e in enumerate(entries)
                          if e.get("type") == "user" and e.get("promptId") == current), None)
    if current_index is None:
        return []
    entry = entries[current_index]
    text = extract_text((entry.get("message") or {}).get("content"))
    if entry.get("isMeta") or is_skippable_user_text(text) or is_task_notification(text):
        return []
    transcript = str(Path(payload["transcript_path"]).resolve())
    abandoned = []
    for path in CACHE_DIR.glob("prompt-*.json"):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                continue
            checkpoint = record.get("checkpoint")
            prompt = record.get("prompt_id")
            if (record.get("session_id") != payload.get("session_id") or not prompt
                    or record.get("owner") != prompt_owner()
                    or prompt in users or not isinstance(checkpoint, dict)
                    or checkpoint.get("path") != transcript):
                continue
            anchor_index = positions.get(checkpoint.get("anchor"))
            if anchor_index is not None and anchor_index < current_index:
                # Queued text may still be waiting for delivery. Its absence
                # from user records is not abandonment; require a receipt below.
                if any(e.get("type") == "queue-operation" and e.get("operation") == "enqueue"
                       and extract_text(e.get("content")).strip() == record.get("prompt", "").strip()
                       for e in entries[anchor_index + 1:]):
                    continue
                abandoned.append(prompt)
        except (OSError, ValueError):
            continue
    return abandoned


def resolved_queued_prompts(payload, entries):
    """Retire old hook IDs only after their queued text was actually consumed.

    Claude can deliver a queued prompt under a new prompt ID or attach it to
    the current turn. Match exact text after the saved transcript checkpoint,
    in this pane/session, and wait for a Stop with no background tasks. A stale
    Stop, a queue entry alone, or ambiguous duplicate deliveries releases nothing.
    """
    if payload.get("background_tasks") != [] or not payload.get("transcript_path"):
        return []
    turns = [e for e in entries if e.get("type") == "user" and e.get("promptId")
             and not e.get("isSidechain") and extract_text((e.get("message") or {}).get("content"))]
    if not turns or turns[-1]["promptId"] != payload.get("prompt_id"):
        return []
    transcript = str(Path(payload["transcript_path"]).resolve())
    positions = {e.get("uuid"): i for i, e in enumerate(entries) if e.get("uuid")}
    receipts = {(e.get("commandUuid"), e.get("deliveryId")) for e in entries
                if e.get("type") == "queue-operation" and e.get("operation") == "remove"
                and e.get("reason") == "absorbed_mid_turn"}
    deliveries = {}
    for i, entry in enumerate(entries):
        text = submission_text(entry)
        if text and entry.get("promptId"):
            deliveries.setdefault(text, []).append((i, ("prompt", entry["promptId"])))
            continue
        attachment = entry.get("attachment")
        if (entry.get("type") != "attachment" or entry.get("isSidechain")
                or not isinstance(attachment, dict)):
            continue
        origin = attachment.get("origin")
        source, delivery = attachment.get("source_uuid"), attachment.get("delivery_id")
        text = attachment.get("prompt")
        if (attachment.get("type") == "queued_command" and attachment.get("commandMode") == "prompt"
                and isinstance(origin, dict) and origin.get("kind") == "human"
                and isinstance(text, str) and text.strip() and source and delivery
                and (source, delivery) in receipts):
            deliveries.setdefault(text.strip(), []).append((i, ("attachment", delivery)))

    groups = {}
    for path in CACHE_DIR.glob("prompt-*.json"):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                continue
            text, prompt = record.get("prompt"), record.get("prompt_id")
            if (record.get("session_id") != payload.get("session_id") or not prompt
                    or record.get("owner") != prompt_owner() or not isinstance(text, str) or not text.strip()):
                continue
            text = text.strip()
            if any(e.get("promptId") == prompt and submission_text(e) for e in entries):
                continue
            checkpoint = record.get("checkpoint")
            anchor = (positions.get(checkpoint.get("anchor")) if isinstance(checkpoint, dict)
                      and checkpoint.get("path") == transcript else None)
            matches = {identity for i, identity in deliveries.get(text, [])
                       if anchor is not None and i > anchor}
            groups.setdefault(text, []).append((prompt, matches))
        except (OSError, ValueError):
            continue

    resolved = []
    for group in groups.values():
        # Two identical queued submissions require two distinct receipts.
        # Do not arbitrarily clear one when we cannot identify which was delivered.
        assigned = {}
        choices = dict(group)

        def match(prompt, seen):
            for delivery in choices[prompt] - seen:
                seen.add(delivery)
                if delivery not in assigned or match(assigned[delivery], seen):
                    assigned[delivery] = prompt
                    return True
            return False

        if all(match(prompt, set()) for prompt, _ in group):
            resolved.extend(prompt for prompt, _ in group)
    return resolved


def remembered_prompt(payload):
    path = prompt_path(payload)
    if path is None:
        return None
    try:
        return json.loads(path.read_text()).get("prompt")
    except (OSError, ValueError):
        return None


def forget_prompt(payload):
    path = prompt_path(payload)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
