"""Optional completion-notification bridge to tmux-agent-indicator.

Register a message digest before sending it. The indicator matches the
reviewer's UserPromptSubmit and Stop turn IDs; keystrokes are not receipts.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from kibitz_hook_common import CACHE_DIR, log


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


def remember_prompt(payload):
    path = prompt_path(payload)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"prompt": payload.get("prompt", "")}, stream)
        temporary.replace(path)
    except OSError as error:
        log(f"prompt capture failed: {type(error).__name__}")


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
