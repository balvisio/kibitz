#!/usr/bin/env python3
"""Find a saved reviewer by the host's explicit name, without launching an agent.

Print the session UUID (empty for fresh), launch directory, and new-session name
as NUL-delimited fields. This module does not depend on tmux-agent-indicator.
"""
from dataclasses import dataclass
from contextlib import closing
import json
import mmap
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from typing import Optional


@dataclass(frozen=True)
class Session:
    agent: str
    id: str
    name: str
    cwd: str
    path: Optional[Path] = None


@dataclass(frozen=True)
class Launch:
    cwd: str
    session_id: str = ""
    name: str = ""


class AmbiguousMatch(Exception):
    pass


def session_id(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def text(value):
    return value if isinstance(value, str) else ""


def same_directory(left, right):
    return bool(left and right) and Path(left).resolve() == Path(right).resolve()


def check_deadline(deadline):
    if time.monotonic() > deadline:
        raise TimeoutError("saved-session lookup timed out")


def codex_sessions(root, deadline=None):
    """Prefer the explicit name column; title/preview may contain generated text."""
    databases = sorted(root.glob("state_*.sqlite"),
                       key=lambda path: int(path.stem[6:]) if path.stem[6:].isdigit() else -1,
                       reverse=True)
    if databases:
        try:
            with closing(sqlite3.connect(databases[0].resolve().as_uri() + "?mode=ro", uri=True,
                                         timeout=0.2)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
                if {"id", "name", "cwd", "rollout_path", "archived"} <= columns:
                    return [Session("codex", sid, text(name), text(cwd), Path(path))
                            for identity, name, cwd, path in db.execute(
                                "SELECT id, name, cwd, rollout_path FROM threads WHERE archived = 0")
                            if (sid := session_id(identity)) and isinstance(path, str)]
        except sqlite3.Error:
            # An unavailable database must not resurrect an obsolete index name.
            raise RuntimeError("Codex session metadata is unavailable") from None

    # Codex versions without the separate name column keep explicit names in
    # this append-only index. A later rename invalidates the previous name.
    names = {}
    index = root / "session_index.jsonl"
    if not index.is_file():
        return []
    with index.open() as stream:
        for line in stream:
            if deadline is not None:
                check_deadline(deadline)
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (sid := session_id(row.get("id"))):
                names[sid] = text(row.get("thread_name"))
    paths = {}
    for path in (root / "sessions").glob("*/*/*/rollout-*.jsonl"):
        if deadline is not None:
            check_deadline(deadline)
        identity = session_id(path.stem[-36:])
        if identity in names:
            paths.setdefault(identity, []).append(path)
    sessions = []
    for sid, name in names.items():
        if deadline is not None:
            check_deadline(deadline)
        for path in paths.get(sid, []):
            try:
                with path.open("rb") as stream:
                    row = json.loads(stream.readline(1024 * 1024))
                meta = row.get("payload", {})
                if row.get("type") == "session_meta" and meta.get("id") == sid:
                    sessions.append(Session("codex", sid, name, text(meta.get("cwd")), path))
                    break
            except (OSError, ValueError, AttributeError):
                continue
    return sessions


def process_snapshot():
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,lstart=,comm="], text=True,
        env=dict(os.environ, TZ="UTC", LC_ALL="C"), stderr=subprocess.DEVNULL, timeout=2)
    processes = {}
    for line in output.splitlines():
        parts = line.split(None, 7)
        if len(parts) == 8 and parts[0].isdigit() and parts[1].isdigit():
            processes[int(parts[0])] = (int(parts[1]), " ".join(parts[2:7]))
    return processes


def has_ancestor(pid, ancestor, processes):
    seen = set()
    while pid in processes and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        pid = processes[pid][0]
    return False


def pid_domain():
    if sys.platform == "linux":
        machine = Path("/etc/machine-id").read_text().strip()
        return "linux:" + machine + ":" + os.readlink("/proc/self/ns/pid")
    return sys.platform


def process_start(pid, processes):
    if sys.platform == "linux":
        # The field after the last ')' begins with field 3 (process state).
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return processes[pid][1]


def claude_host(root, pane):
    """Require a live native record belonging to this exact pane/process tree."""
    pane_pid = int(subprocess.check_output(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_pid}"],
        text=True, stderr=subprocess.DEVNULL, timeout=2).strip())
    processes = process_snapshot()
    domain = pid_domain()
    found = []
    for path in (root / "sessions").glob("*.json"):
        try:
            row = json.loads(path.read_text())
            if not isinstance(row, dict) or str(row.get("tmux", "")).rsplit(".", 1)[-1] != pane:
                continue
            pid = int(row["pid"])
            sid = session_id(row.get("sessionId"))
            if (not sid or row.get("kind") != "interactive" or row.get("pidDomain") != domain
                    or not has_ancestor(pid, pane_pid, processes)
                    or process_start(pid, processes) != " ".join(str(row.get("procStart", "")).split())):
                continue
            name = text(row.get("name")) if row.get("nameSource") == "user" else ""
            found.append(Session("claude", sid, name, text(row.get("cwd"))))
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
    return found[0] if len(found) == 1 else None


def claude_saved_session(path, deadline):
    """Use only custom-title records, ignoring AI titles and derived agent names.

    Search backward without parsing message bodies. Claude usually appends the
    current custom title near the end, but it may be far back in a long session.
    """
    identity = session_id(path.stem)
    if not identity or path.stat().st_size == 0:
        return None
    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        end = len(data)
        name = None
        while end > 0:
            check_deadline(deadline)
            pos = data.rfind(b'"custom-title"', 0, end)
            if pos < 0:
                break
            start = data.rfind(b"\n", 0, pos) + 1
            stop = data.find(b"\n", pos)
            stop = len(data) if stop < 0 else stop
            end = start
            if stop - start > 65536:
                continue
            try:
                row = json.loads(data[start:stop])
            except ValueError:
                continue
            if (isinstance(row, dict) and row.get("type") == "custom-title"
                    and row.get("sessionId") == identity):
                name = text(row.get("customTitle"))
                break
        if not name:
            return None
        # Workspace identity comes from transcript metadata, never from a
        # directory-name encoding that can collide or change across versions.
        stream.seek(0)
        while stream.tell() < 1024 * 1024:
            check_deadline(deadline)
            line = stream.readline(1024 * 1024)
            if not line:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("sessionId") == identity and text(row.get("cwd")):
                return Session("claude", identity, name, text(row["cwd"]), path)
    return None


def claude_sessions(root, deadline):
    sessions = []
    for path in (root / "projects").glob("*/*.jsonl"):
        check_deadline(deadline)
        try:
            session = claude_saved_session(path, deadline)
        except (OSError, ValueError):
            continue
        if session:
            sessions.append(session)
    return sessions


def matching_session(host, reviewer, candidates):
    if not host or not host.name:
        return None
    matches = {session.id: session for session in candidates
               if session.agent == reviewer and session.name == host.name
               and same_directory(session.cwd, host.cwd)
               and session.path is not None and session.path.is_file()
               and (session.agent, session.id) != (host.agent, host.id)}
    if len(matches) > 1:
        raise AmbiguousMatch
    return next(iter(matches.values())) if len(matches) == 1 else None


def resolve(reviewer):
    launch = Launch(str(Path.cwd()))
    pane = os.environ.get("TMUX_PANE", "")
    if not pane or not os.environ.get("TMUX"):
        return launch
    try:
        deadline = time.monotonic() + 5
        claude_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
        codex_root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        # Codex supplies the exact thread identity to commands it runs. Claude
        # tool calls set CLAUDECODE, even if launched from a Codex environment.
        identity = session_id(os.environ.get("CODEX_THREAD_ID"))
        codex = None
        if identity and os.environ.get("CLAUDECODE") != "1":
            codex = codex_sessions(codex_root, deadline)
            host = next((session for session in codex if session.id == identity), None)
        else:
            host = claude_host(claude_root, pane)
        if not host or not Path(host.cwd).is_absolute() or not Path(host.cwd).is_dir():
            return launch
        # A tool's temporary working directory does not redefine the project.
        launch = Launch(host.cwd)
        if not host.name:
            return launch
        if reviewer == "codex":
            candidates = codex if codex is not None else codex_sessions(codex_root, deadline)
        else:
            candidates = claude_sessions(claude_root, deadline)
        check_deadline(deadline)
        session = matching_session(host, reviewer, candidates)
        if session:
            return Launch(session.cwd, session.id)
        return Launch(host.cwd, name=host.name)
    except AmbiguousMatch:
        print("[kibitz] Multiple reviewer sessions match this name in this directory; starting fresh without a name.",
              file=sys.stderr)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"[kibitz] Cannot check saved reviewer sessions ({type(error).__name__}); starting fresh.",
              file=sys.stderr)
    return launch


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("codex", "claude"):
        return 2
    launch = resolve(sys.argv[1])
    sys.stdout.buffer.write(launch.session_id.encode("ascii") + b"\0" + os.fsencode(launch.cwd)
                            + b"\0" + launch.name.encode("utf-8") + b"\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
