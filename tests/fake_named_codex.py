"""Local app-server fixture. Never invokes a real agent or model."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

CREATED = "01900000-1111-7222-8333-000000000004"


def app_server():
    home = Path(os.environ["HOME"])
    mode = os.environ.get("KIBITZ_TEST_CODEX_SCENARIO", "")
    thread = dict(id=CREATED, cwd=os.getcwd(), name=None)
    def send(row):
        print(json.dumps(row), flush=True)
    def persist():
        if not os.environ.get("KIBITZ_TEST_PERSIST_CREATED"):
            return
        root = Path(os.environ["CODEX_HOME"])
        path = root / "sessions/2026/11/16" / ("rollout-test-" + CREATED + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(type="session_meta", payload=dict(id=CREATED, cwd=os.getcwd()))) + "\n")
        with sqlite3.connect(root / "state_5.sqlite") as db:
            db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, "
                       "preview TEXT, cwd TEXT, rollout_path TEXT, archived INTEGER)")
            db.execute("INSERT OR REPLACE INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (CREATED, thread["name"], "", "", os.getcwd(), str(path), 0))
    for line in sys.stdin:
        row = json.loads(line)
        with (home / "codex-start-calls.jsonl").open("a") as log:
            log.write(json.dumps(row) + "\n")
        method = row.get("method")
        if not method or "id" not in row:
            continue
        if mode == "timeout":
            time.sleep(10)
            continue
        if mode == method:
            send(dict(id=row["id"], error=dict(code=-1, message="fixture error")))
            continue
        if method == "initialize":
            send(dict(id=row["id"], result={}))
            continue
        if method == "thread/start":
            assert row["params"]["ephemeral"] is False
            assert row["params"]["cwd"] == os.getcwd()
            if mode == "invalid-id":
                thread["id"] = "not-a-uuid;echo unsafe"
            send(dict(method="thread/status/changed", params={}))
            send(dict(id=row["id"], result=dict(thread=thread)))
        elif method == "thread/name/set":
            thread["name"] = "wrong" if mode == "wrong-name" else row["params"]["name"]
            persist()
            send(dict(id=row["id"], result={}))
        elif method == "thread/read":
            send(dict(id=row["id"], result=dict(thread=thread)))
        elif method == "thread/archive":
            send(dict(id=row["id"], result={}))
        else:
            raise AssertionError("unexpected request: " + method)


if __name__ == "__main__":
    assert sys.argv[1] == "app-server", sys.argv
    assert sys.argv[-2:] == ["--disable", "codex_hooks"], sys.argv
    app_server()
