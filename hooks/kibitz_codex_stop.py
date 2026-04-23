#!/usr/bin/env python3
"""kibitz_codex_stop — Codex Stop hook that persists last_assistant_message so
`kibitz relay` can forward it to the host pane.

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


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin json: {e}")
        return 0

    if not isinstance(payload, dict):
        log(f"payload not a dict: {type(payload).__name__}")
        return 0

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
