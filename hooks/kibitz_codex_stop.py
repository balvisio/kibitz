#!/usr/bin/env python3
"""kibitz_codex_stop — Codex hooks (SessionStart, UserPromptSubmit and Stop).

On every event it records which rollout log belongs to this tmux pane
(pane-<id>.rollout in the cache dir), so the Claude-side hook can read the
rollout's tail and tell whether codex is mid-turn before delivering. The
mapping includes the tmux server and owning agent process identity; stale
mappings are ignored. Busy means Tab (queue), idle means Enter.

On Stop it also persists last_assistant_message so `kibitz relay` can
forward it to the host pane.

On UserPromptSubmit it handles explicit /tee prompts through the shared
prompt handler; ordinary Codex prompts and replies remain local.

Invoked with the hook payload on stdin. Exits 0 with empty stdout
unconditionally so hook failures never block the codex session; errors go
to ~/.cache/kibitz/log.

Keyed by CODEX_THREAD_ID (inherited from codex) so `kibitz relay` — running
in the same codex shell — can look up its own thread's payload without
TMUX_PANE or any other undocumented env plumbing.
"""
import json
import os
import sys
import time
import kibitz_hook_common as common
from pathlib import Path

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kibitz"
LOG_PATH = CACHE_DIR / "log"


def log(msg):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} codex-stop: {msg}\n")
    except Exception:
        pass


def record_rollout(payload):
    """Remember which rollout log belongs to this pane. Codex passes the path
    as transcript_path; fall back to finding it by session id."""
    if not common.valid_source():
        return
    context = common.source_context()
    pane = context["pane"].lstrip("%")
    agent = next(((pid, info) for pid, info in context["ancestors"].items()
                  if Path(info["command"]).name == "codex"), None)
    if not agent:
        return
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        session = payload.get("session_id") or os.environ.get("CODEX_THREAD_ID") or ""
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        matches = sorted(root.glob(f"sessions/*/*/*/rollout-*-{session}.jsonl")) if session else []
        if not matches:
            return
        path = str(matches[-1])
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = CACHE_DIR / f"pane-{pane}.rollout.{os.getpid()}.tmp"
        pid, info = agent
        tmp_path.write_text(json.dumps(dict(path=path, server=context["server"],
            identity=[context["pane_pid"], context["tty"], pid, info["started"], "codex"])))
        tmp_path.replace(CACHE_DIR / f"pane-{pane}.rollout")
    except Exception as e:
        log(f"rollout mapping write failed: {e}")


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin json: {e}")
        return 0

    if not isinstance(payload, dict):
        log(f"payload not a dict: {type(payload).__name__}")
        return 0

    if (payload.get("hook_event_name") == "UserPromptSubmit"
            and payload.get("transcript_path", "") is None):
        return 0
    if not common.valid_source():
        return 0
    record_rollout(payload)

    if payload.get("hook_event_name") == "UserPromptSubmit":
        from kibitz_hook_user_prompt_submit import handle_prompt
        return handle_prompt(payload, agent="codex")

    message = payload.get("last_assistant_message")
    if not isinstance(message, str) or not message.strip():
        return 0

    thread_id = os.environ.get("CODEX_THREAD_ID") or payload.get("session_id") or ""
    if not thread_id:
        log("no CODEX_THREAD_ID in env and no session_id in payload")
        return 0

    turn_id = payload.get("turn_id") or ""

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"cache dir create failed: {e}")
        return 0

    msg_path = CACHE_DIR / f"codex-{thread_id}.msg"
    tmp_path = msg_path.with_suffix(".msg.tmp")
    try:
        with tmp_path.open("w") as f:
            json.dump(
                {
                    "turn_id": turn_id,
                    "session_id": payload.get("session_id", ""),
                    "message": message,
                },
                f,
            )
        tmp_path.replace(msg_path)
    except Exception as e:
        log(f"write failed for {msg_path}: {e}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
