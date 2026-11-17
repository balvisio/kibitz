#!/usr/bin/env python3
"""Create and name an empty Codex thread before its TUI starts.

Use Codex's local app-server protocol, never edits to its database or terminal
keystrokes. No turn is started and no prompt is submitted to a model.
"""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid


class AppServer:
    def __init__(self, config_args, timeout=30):
        self.deadline = time.monotonic() + timeout
        self.counter = 0
        self.buffer = b""
        env = os.environ.copy()
        for key in ("TMUX", "TMUX_PANE", "CODEX_THREAD_ID", "CLAUDECODE"):
            env.pop(key, None)
        # The real TUI runs the user's hooks on resume. The temporary metadata
        # session must not fire duplicate SessionStart/SessionEnd hooks.
        self.proc = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://", *config_args,
             "--disable", "codex_hooks"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)

    def send(self, row):
        self.proc.stdin.write(json.dumps(row).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def receive(self):
        while b"\n" not in self.buffer:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Codex startup timed out")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Codex app-server exited during startup")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def rpc(self, method, params):
        self.counter += 1
        request_id = self.counter
        self.send(dict(id=request_id, method=method, params=params))
        while True:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Codex startup timed out")
            row = self.receive()
            if "method" in row:
                if "id" in row:
                    # Metadata setup cannot approve or run a requested action.
                    self.send(dict(id=row["id"], error=dict(
                        code=-32601, message="Kibitz startup does not handle server requests")))
                continue
            if row.get("id") == request_id:
                if "error" in row:
                    raise RuntimeError(f"Codex {method} failed: {row['error'].get('message', 'unknown error')}")
                return row["result"]

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait()
            except ProcessLookupError:
                self.proc.wait()
        finally:
            self.selector.close()
            self.proc.stdout.close()


def create_named_thread(name, config_args=(), timeout=30):
    if not name.strip():
        raise ValueError("the reviewer name is empty")
    client = AppServer(config_args, timeout)
    created = None
    try:
        client.rpc("initialize", {"clientInfo": {"name": "kibitz", "version": "1"}})
        client.send({"method": "initialized"})
        thread = client.rpc("thread/start", {"cwd": str(Path.cwd()), "ephemeral": False})["thread"]
        created = str(uuid.UUID(thread["id"]))
        client.rpc("thread/name/set", {"threadId": created, "name": name})
        saved = client.rpc("thread/read", {"threadId": created})["thread"]
        if (saved.get("id") != created or saved.get("name") != name
                or Path(saved.get("cwd", "")).resolve() != Path.cwd()):
            raise RuntimeError("Codex did not confirm the reviewer name and directory")
        return created
    except Exception:
        if created:
            # Only the empty thread owned by this failed attempt is archived.
            # Never fall back to a second new conversation after a partial start.
            try:
                client.deadline = time.monotonic() + 2
                client.rpc("thread/archive", {"threadId": created})
            except Exception:
                pass
        raise
    finally:
        client.close()


def main():
    if len(sys.argv) < 2:
        return 2
    try:
        print(create_named_thread(sys.argv[1], sys.argv[2:]))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(f"[kibitz] Could not create named Codex reviewer: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
