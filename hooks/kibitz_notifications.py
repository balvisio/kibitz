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
                if entry.get("promptId") == payload.get("prompt_id"):
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
    users = {e.get("promptId") for e in entries if e.get("type") == "user"}
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
                abandoned.append(prompt)
        except (OSError, ValueError):
            continue
    return abandoned


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
