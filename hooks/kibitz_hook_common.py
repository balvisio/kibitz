"""Shared helpers for Kibitz's Claude Code and Codex hooks.

Imported by kibitz_hook_stop.py and kibitz_hook_user_prompt_submit.py. Lives
alongside them in ~/.local/bin/ after install; Python's default sys.path[0]
(the invoked script's directory) makes `import kibitz_hook_common` resolve.
"""
import json
import hashlib
import os
import fcntl
import re
import subprocess
import sys
import time
import uuid
import stat
import kibitz_processes as processes
from pathlib import Path

REVIEWER_LABELS = ("codex", "claude-reviewer")
LOG_PATH = Path.home() / ".claude" / "kibitz-hook.log"
LAST_FORWARD_PATH = Path.home() / ".claude" / "kibitz-last.txt"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "kibitz"
RUNTIME_DIR = Path("/tmp") / ("kibitz-" + str(os.getuid()))

# Case-sensitive. /MUTE, /Tee, etc. fall through as normal text — keeping the
# surface area narrow avoids accidentally swallowing content that merely
# resembles a directive.
KIBITZ_DIRECTIVES = ("/mute", "/tee")

# User-entry content Claude Code records for a `! shell` command. A reply it
# triggers is never forwarded automatically — `kibitz relay` sends it by hand.
_SHELL_PREFIXES = (
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)

# User-entry content that isn't a real prompt: the shell records above, the
# three entries written for a slash command (/model, /effort and /fast run
# while Claude is still working, so these can land mid-turn), tool errors.
# Any user entry whose stripped text starts with one of these — or a leading
# '/' — is never used as the USER text of a forwarded exchange.
_SKIP_USER_PREFIXES = _SHELL_PREFIXES + (
    "<local-command-caveat>",
    "<command-name>",
    "<local-command-stdout>",
    "<tool_use_error>",
)


def log(msg):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def extract_text(content):
    """Content may be a string or a list of content blocks; return joined text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                s = block.strip()
                if s:
                    parts.append(s)
            elif isinstance(block, dict) and block.get("type") == "text":
                s = (block.get("text") or "").strip()
                if s:
                    parts.append(s)
        return "\n".join(parts).strip()
    return ""


def is_skippable_user_text(text):
    """Return True for user entries that aren't real prompts (tool results,
    bash wrappers, Claude Code slash commands). A bare /mute or /tee is *not*
    skipped — parse_directive in the caller handles those."""
    s = text.lstrip()
    if not s:
        return True
    if s.startswith("/"):
        if s.rstrip() in KIBITZ_DIRECTIVES:
            return False
        return True
    return s.startswith(_SKIP_USER_PREFIXES)


def is_command_text(text):
    """Return True for a user entry that is a `! shell` command record or a
    typed slash command (bare /mute and /tee are directives, not commands)."""
    s = text.lstrip()
    if s.startswith("/"):
        return s.rstrip() not in KIBITZ_DIRECTIVES
    return s.startswith(_SHELL_PREFIXES)


def is_task_notification(text):
    """A background-task wakeup continues its owning human prompt."""
    return text.lstrip().startswith("<task-notification>")


def is_reviewer_originated(text):
    """Return True if the user message was relayed from the reviewer via
    `kibitz send` — those carry a '[kibitz from:<agent>]' header. Replies to
    such messages should not be forwarded back: the reviewer already saw its
    own outbound text, and a round-trip produces a ping-pong loop with no new
    information."""
    return text.lstrip().startswith("[kibitz from:")


def parse_directive(text):
    """Return (cleaned_text, directive) where directive is "mute", "tee", or "".

    Directives must appear at the very end of the message, preceded by
    whitespace or be the entire message — so `path/to/mute` or `/muted` don't
    trigger. Match is case-sensitive."""
    stripped = text.rstrip()
    for token in KIBITZ_DIRECTIVES:
        if stripped.endswith(token):
            prefix = stripped[: -len(token)]
            if not prefix or prefix[-1].isspace():
                return prefix.rstrip(), token[1:]
    return text, ""


def current_pane_label():
    # Target $TMUX_PANE explicitly: `display-message -p` without `-t` returns
    # the *focused* pane's @name, so when the reviewer pane has focus (e.g.
    # right after `kibitz send`), the host's hook would see the reviewer label
    # and bail out thinking it was running inside the reviewer.
    pane = os.environ.get("TMUX_PANE", "")
    cmd = ["tmux", "display-message", "-p"]
    if pane:
        cmd += ["-t", pane]
    cmd += ["#{@name}"]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def resolve_reviewer():
    """Find a reviewer pane in the host pane's tmux window only.

    Use the pane ID and recorded host pointer; labels alone are server-wide
    and would cross-wire windows containing the same reviewer agent.

    Returns (pane_id, label) or None.
    """
    host_pane = os.environ.get("TMUX_PANE", "")
    if not valid_source() or not host_pane:
        return None
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-t", host_pane, "-F", "#{pane_id} #{@name} #{@kibitz_host_pane}"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except Exception:
        return None
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            pane_id, label, host = parts
            if label in REVIEWER_LABELS and host == host_pane:
                return pane_id, label
    return None


def resolve_counterpart(agent):
    """Return (pane, agent, is_reviewer) for an explicit /tee in either pane.

    Resolve the recorded pair within the source pane's window, never the
    focused pane or a server-wide label. A missing/stale link sends nothing.
    """
    source = os.environ.get("TMUX_PANE", "")
    if not valid_source() or not source:
        return None
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", source, "-F",
             "#{pane_id}\t#{@name}\t#{@kibitz_host_pane}\t#{pane_dead}"],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    panes = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 4 and fields[3] == "0":
            panes[fields[0]] = fields[1:3]
    if source not in panes:
        return None
    host = panes[source][1]
    if host:
        if host != source and host in panes:
            # A host can run either agent, including the reviewer's own kind.
            # The guarded sender determines its type from the live process.
            return host, None, False
        return None
    reviewers = [(pane, "codex" if label == "codex" else "claude", True)
                 for pane, (label, host) in panes.items()
                 if pane != source and host == source and label in REVIEWER_LABELS]
    return reviewers[0] if len(reviewers) == 1 else None


_LIFECYCLE = ("task_started", "task_complete", "turn_aborted")


def last_lifecycle_event(path):
    """Type of the latest task_started / task_complete / turn_aborted event in
    a codex rollout log, scanning backward block by block so a long tool
    result after the event cannot hide it. None if there is none."""
    with path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(65536, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            lines = buf.split(b"\n")
            first_complete = 0 if pos == 0 else 1
            for line in reversed(lines[first_complete:]):
                if not any(kind.encode() in line for kind in _LIFECYCLE):
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "event_msg":
                    kind = (event.get("payload") or {}).get("type")
                    if kind in _LIFECYCLE:
                        return kind
            buf = lines[0] if first_complete else b""
    return None


class DeliveryRefused(RuntimeError):
    """A delivery stopped, with the last stage known to have reached tmux."""

    def __init__(self, reason, stage="not_pasted", transient=False):
        super().__init__(reason)
        self.stage = stage
        self.transient = transient


_SOURCE_CONTEXT = None


def source_context():
    """Prove this invocation descends from the pane claimed by its environment.

    Daemon hooks may inherit another pane's environment. A label or TMUX_PANE
    alone is never authority to route messages or write pane-owned metadata.
    Cache only for this short-lived invocation, including its OS process ID.
    """
    global _SOURCE_CONTEXT
    pane = os.environ.get("TMUX_PANE", "")
    server = os.environ.get("TMUX", "").rsplit(",", 1)[0]
    key = (os.getpid(), server, pane)
    if _SOURCE_CONTEXT and _SOURCE_CONTEXT[0] == key:
        return _SOURCE_CONTEXT[1]
    if not server or not re.fullmatch(r"%[0-9]+", pane):
        raise DeliveryRefused("source is not inside an identifiable tmux pane")
    try:
        pid, tty, dead = delivery_tmux("display-message", "-p", "-t", pane,
                                      "#{pane_pid}\t#{pane_tty}\t#{pane_dead}").strip().split("\t")
        records = processes.ancestors(os.getpid())
        child, ancestors = os.getpid(), {}
        while child not in ancestors:
            if child == int(pid) and dead == "0":
                if child in records:
                    ancestors[child] = records[child]
                context = dict(pane=pane, pane_pid=pid, tty=tty, server=server, ancestors=ancestors)
                _SOURCE_CONTEXT = key, context
                return context
            if child not in records:
                break
            ancestors[child] = records[child]
            child = records[child]["parent"]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise DeliveryRefused("could not verify source pane: " + str(error), transient=True) from error
    raise DeliveryRefused("hook process does not belong to its claimed tmux pane")


def valid_source():
    for attempt in range(2):
        try:
            source_context()
            return True
        except DeliveryRefused as error:
            if error.transient and attempt == 0:
                continue
            report_delivery_failure(error)
            return False


def reviewer_busy(pane_id, target=None):
    """Trust a rollout only while its recorded agent process still owns the pane."""
    try:
        target = target or delivery_target(pane_id, "codex")
        data = json.loads((CACHE_DIR / f"pane-{pane_id.lstrip('%')}.rollout").read_text())
        if (data["server"] != os.environ.get("TMUX", "").rsplit(",", 1)[0]
                or data["identity"] != list(target["identity"])):
            return False
        return last_lifecycle_event(Path(data["path"])) == "task_started"
    except (OSError, ValueError, KeyError, TypeError, DeliveryRefused, subprocess.SubprocessError):
        return False


def delivery_tmux(*args, input=None):
    return subprocess.check_output(["tmux", *args], input=input, text=True,
                                   stderr=subprocess.PIPE, timeout=5)


def terminal_processes(tty):
    return processes.terminal_processes(tty)


def claude_delivery_status(pane, processes):
    """Match Claude's native status to a live process, including PID reuse."""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    domain = sys.platform
    if sys.platform == "linux":
        try:
            machine = Path("/etc/machine-id").read_text().strip()
        except OSError:
            machine = ""
        try:
            namespace = os.readlink("/proc/self/ns/pid")
        except OSError:
            return {}
        domain = "linux:" + machine + ":" + namespace
    found = {}
    for path in (root / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text())
            pid = int(data["pid"])
            if (pid not in processes or data.get("kind") != "interactive"
                    or data.get("pidDomain") != domain
                    or str(data.get("tmux", "")).rsplit(".", 1)[-1] != pane):
                continue
            started = (Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
                       if sys.platform == "linux" else processes[pid]["started"])
            if started == " ".join(str(data.get("procStart", "")).split()):
                found[pid] = data.get("status")
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue
    return found


_ANSI = re.compile(r'\x1b\[([0-9;]*)m')
_HYPERLINK = re.compile(r'\x1b\]8;[^\x1b\x07;]*;[^\x1b\x07]*(?:\x1b\\|\x07)')
_QUESTION_FOOTER = re.compile(r'(?:enter to confirm|esc to cancel|to submit answer|to clear notes|to add notes|enter select|esc back)', re.I)
_NORMAL_FOOTER = re.compile(r'(?:\?\s+for shortcuts|\b\d+%\s+(?:left|context)|\bcontext left|tab\s+to queue)', re.I)


def screen_rows(screen):
    """Text and background at the composer's input column; colours delimit Codex's box."""
    rows, backgrounds = [], []
    background = None
    # OSC 8 carries link metadata, not visible cells. Keep its label and SGR
    # colours so the composer boundaries and tmux text checks use visible text.
    for raw in _HYPERLINK.sub('', screen).splitlines():
        text, cells, offset = '', [], 0
        for match in _ANSI.finditer(raw):
            chunk = raw[offset:match.start()]
            text += chunk
            cells.extend([background] * len(chunk))
            codes = [int(value or '0') for value in match[1].split(';')]
            i = 0
            while i < len(codes):
                code = codes[i]
                if code in (0, 49):
                    background = None
                elif 40 <= code <= 47 or 100 <= code <= 107:
                    background = (code,)
                elif code in (38, 48) and i + 1 < len(codes):
                    count = 2 if codes[i + 1] == 5 else 4 if codes[i + 1] == 2 else 0
                    if code == 48:
                        background = tuple(codes[i + 1:i + 1 + count])
                    i += count
                i += 1
            offset = match.end()
        chunk = raw[offset:]
        text += chunk
        cells.extend([background] * len(chunk))
        rows.append(text)
        backgrounds.append(cells[min(2, len(cells) - 1)] if cells else background)
    return rows, backgrounds


def composer_start(agent, lines, backgrounds, row, anchor=None):
    footer = ' '.join(lines[row + 1:])
    if _QUESTION_FOOTER.search(footer) or re.search(r'enter to submit.*esc to interrupt', footer, re.I):
        raise DeliveryRefused('target is waiting on a question')
    marker = r'❯' if agent == 'claude' else r'[›❯»]'
    match_prompt = lambda i: re.match(r'^' + marker + r'\s', lines[i])
    if agent == 'codex' and backgrounds[row] is not None:
        start = row
        while start > 0 and backgrounds[start - 1] == backgrounds[row]:
            start -= 1
        # The first marker belongs to the composer; later markers are pasted text.
        candidates = [i for i in range(start, row + 1) if match_prompt(i)]
        if candidates:
            return candidates[0]
        raise DeliveryRefused('target input has no composer marker')
    if anchor is not None:
        start = anchor['composer_row']
        if start <= row and start < len(lines) and match_prompt(start):
            # Only after preflight established this particular input box. Content
            # typed into it is not a UI separator or a numbered permission choice.
            if agent == 'claude' or _NORMAL_FOOTER.search(footer):
                return start
    candidates = [i for i in range(row + 1) if match_prompt(i)]
    if not candidates:
        raise DeliveryRefused('target is not displaying its normal prompt')
    start = candidates[-1]
    if (re.match(r'^\s*' + marker + r'\s+\d+[.)]', lines[start])
            or any(line.lstrip().startswith('─') for line in lines[start + 1:row + 1])):
        raise DeliveryRefused('target is displaying a selection, not a prompt')
    if any(line.strip() and not line.startswith('  ') for line in lines[start + 1:row + 1]):
        raise DeliveryRefused('target input is separated from the composer marker')
    if agent == 'codex' and not _NORMAL_FOOTER.search(footer):
        raise DeliveryRefused('target input has no normal composer footer')
    if agent == 'claude' and start != row and not any(
            re.fullmatch(r'\s*[─━]{3,}\s*', line) for line in lines[row + 1:]):
        raise DeliveryRefused('target input is outside the Claude composer')
    return start


def private_runtime():
    RUNTIME_DIR.mkdir(mode=0o700, exist_ok=True)
    info = RUNTIME_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DeliveryRefused('unsafe Kibitz runtime directory')
    return RUNTIME_DIR


def wait_for_target(pane, expected=None, anchor=None, deadline=None):
    deadline = deadline or time.monotonic() + 3
    while True:
        try:
            return delivery_target(pane, expected, anchor=anchor)
        except DeliveryRefused as error:
            if not error.transient or time.monotonic() >= deadline:
                raise
            time.sleep(.05)


def delivery_target(pane, expected_agent=None, anchor=None):
    """Observe process identity and the live composer, independently of colours."""
    if not re.fullmatch(r"%[0-9]+", pane):
        raise DeliveryRefused("invalid target pane")
    raw = delivery_tmux("display-message", "-p", "-t", pane,
        "#{pane_pid}\t#{pane_tty}\t#{pane_current_command}\t#{pane_dead}\t#{pane_in_mode}"
        "\t#{cursor_y}\t#{cursor_flag}\t#{bracket_paste_flag}\t#{version}\t#{cursor_x}\t#{pane_width}\t#{pane_height}",
        ";", "capture-pane", "-e", "-p", "-t", pane)
    header, screen = raw.split("\n", 1)
    pid, tty, command, dead, mode, cursor, visible, paste, version, column, width, height = header.split("\t")
    release = re.match(r"(\d+)\.(\d+)", version)
    if not release or tuple(map(int, release.groups())) < (3, 7):
        raise DeliveryRefused("tmux 3.7 or newer is required (server is " + version + ")")
    if (dead != "0" or mode != "0" or paste != "1"
            or not re.fullmatch(r"[a-zA-Z0-9_./@()+-]+", command)):
        raise DeliveryRefused("target is not in normal prompt input mode")
    if visible != "1":
        raise DeliveryRefused("target cursor is temporarily hidden", transient=True)
    processes = terminal_processes(tty)

    def belongs(child):
        seen = set()
        while child not in seen:
            if child == int(pid):
                return True
            if child not in processes:
                break
            seen.add(child)
            child = processes[child]["parent"]
        return False

    foreground = {child: info for child, info in processes.items()
                  if info["foreground"] and not info["stopped"] and belongs(child)}
    def claude_binary(name):
        # Claude's native installer executes ~/.local/share/claude/versions/<version>.
        # The native session record still has to verify this exact process birth.
        return name in ('claude', 'node') or bool(re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?', name))
    statuses = (claude_delivery_status(pane, foreground)
                if any(claude_binary(Path(info['command']).name) for info in foreground.values()) else {})
    agents = []
    for child, info in foreground.items():
        name = Path(info["command"]).name
        if name == "codex":
            agents.append((child, "codex", info))
        elif claude_binary(name) and child in statuses:
            agents.append((child, "claude", info))
    # Agent subprocesses (for example Codex MCP inside Claude) do not own the
    # terminal composer. Keep only the outermost foreground interactive agent.
    agent_pids = {item[0] for item in agents}
    def nested(child):
        seen = set()
        parent = foreground[child]['parent']
        while parent in processes and parent not in seen:
            if parent in agent_pids:
                return True
            seen.add(parent)
            parent = processes[parent]['parent']
        return False
    agents = [item for item in agents if not nested(item[0])]
    if len(agents) != 1:
        raise DeliveryRefused("target has no unambiguous foreground Claude or Codex agent")
    child, agent, info = agents[0]
    if expected_agent and agent != expected_agent:
        raise DeliveryRefused("target is running a different agent")
    if agent == "claude" and statuses[child] not in ("idle", "busy", "shell"):
        raise DeliveryRefused("Claude is waiting on a question or is not ready")
    lines, backgrounds = screen_rows(screen)
    row = int(cursor)
    if not 0 <= row < len(lines):
        raise DeliveryRefused("target prompt is not visible", transient=True)
    start = composer_start(agent, lines, backgrounds, row, anchor)
    return dict(pane=pane, pane_pid=pid, tty=tty, command=command, agent=agent,
                identity=(pid, tty, child, info["started"], agent),
                composer="\n".join(lines[start:row + 1]), composer_row=start, screen="\n".join(lines),
                cursor_y=cursor, cursor_x=column, width=width, height=height,
                busy=bool(re.search(r"\([^()]*\b\d+[smh][^()]*[•·]\s*esc to interrupt\)",
                                    " ".join(lines[max(0, start - 6):start]))))


def delivery_command(target, command):
    """Recheck tmux's live flags in the same command queue as the write."""
    checks = ["#{==:#{pane_pid}," + target["pane_pid"] + "}",
              "#{==:#{pane_current_command}," + target["command"] + "}",
              "#{==:#{pane_dead},0}", "#{==:#{pane_in_mode},0}",
              "#{==:#{cursor_flag},1}", "#{==:#{bracket_paste_flag},1}"]
    # A redraw, resize or cursor move after the screen read invalidates it.
    # This narrows the final check/key race; terminal keystrokes are not an
    # atomic agent API or proof that the model accepted the message.
    for field, key in (("cursor_y", "cursor_y"), ("cursor_x", "cursor_x"),
                       ("pane_width", "width"), ("pane_height", "height")):
        checks.append("#{==:#{" + field + "}," + target[key] + "}")
    # tmux checks the observed composer/footer text in the same queue as the
    # write. Format escaping is separate from shell quoting: payload text is data.
    lines = target['screen'].splitlines()
    for row in [target['composer_row']] + list(range(int(target['cursor_y']) + 1, len(lines))):
        line = lines[row].rstrip()
        if not line:
            continue
        if (target['agent'] == 'claude' and row == target['composer_row']
                and line == '❯'):
            # Claude's empty composer is "❯\u00a0". tmux's search trims bytes
            # with isspace(), corrupting the final UTF-8 character on macOS;
            # even an unanchored regex then fails. Omit only this empty row;
            # the native Claude status check, cursor/geometry checks above
            # and all footer checks below still gate the delivery.
            continue
        pattern = '^' + re.escape(line) + '$'
        pattern = pattern.replace('#', '##').replace(',', '#,').replace('}', '#}')
        first = next(i for i, other in enumerate(lines) if other.rstrip() == line) + 1
        checks.append('#{==:#{C/r:' + pattern + '},' + str(first) + '}')
    condition = checks.pop()
    for check in checks:
        condition = "#{&&:" + check + "," + condition + "}"
    result = delivery_tmux("if-shell", "-F", "-t", target["pane"], condition,
                           command + " ; display-message -p kibitz-delivered")
    if result.strip() != "kibitz-delivered":
        raise DeliveryRefused("target changed before delivery", transient=True)


def forward_prompt(target, message, expected_agent=None, force=False, before_paste=None):
    """Guard every delivery, including automatic forwarding and manual relays.

    Reserve work only after preflight, under the target's delivery lock, but
    before the paste can trigger a receiver hook. Failure stages describe
    evidence, not assumptions about whether a timed-out command ran.
    """
    stage = "not_pasted"
    buffer = None
    try:
        source_context()
        if any(ord(char) < 32 and char not in "\n\t" for char in message):
            raise DeliveryRefused("message contains terminal control characters")
        directory = private_runtime()
        socket = os.environ.get("TMUX", "").rsplit(",", 2)[0]
        key = hashlib.sha256((socket + "\0" + target).encode()).hexdigest()
        lock_path = directory / ("delivery-" + key + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeliveryRefused("another message is being delivered") from None
            before = wait_for_target(target, expected_agent)
            buffer = "kibitz-" + uuid.uuid4().hex
            delivery_tmux("load-buffer", "-b", buffer, "-", input=message)
            if before_paste:
                before_paste()
                refreshed = wait_for_target(target, expected_agent)
                if refreshed['identity'] != before['identity']:
                    raise DeliveryRefused('target agent changed while reserving delivery')
                before = refreshed
            deadline = time.monotonic() + 3
            while True:
                # A timeout may mean the paste ran. Only an explicit failed
                # in-server condition proves nothing reached the target.
                stage = "paste_unknown"
                try:
                    delivery_command(before, f"paste-buffer -p -d -b {buffer} -t {target}")
                    break
                except DeliveryRefused as error:
                    stage = "not_pasted"
                    if not error.transient or time.monotonic() >= deadline:
                        raise
                    refreshed = wait_for_target(target, expected_agent, deadline=deadline)
                    if refreshed['identity'] != before['identity']:
                        raise DeliveryRefused('target agent changed before paste')
                    before = refreshed
                    time.sleep(.05)
            stage = "pasted"
            deadline = time.monotonic() + 3
            while True:
                after = wait_for_target(target, before["agent"], anchor=before, deadline=deadline)
                if after["identity"] != before["identity"]:
                    raise DeliveryRefused("target agent changed during delivery")
                if after["composer"] == before["composer"]:
                    if time.monotonic() >= deadline:
                        raise DeliveryRefused("paste was not visible in the target prompt")
                    time.sleep(.05)
                    continue
                busy = after["agent"] == "codex" and (after["busy"] or reviewer_busy(target, after))
                submit = "Tab" if busy and not force else "Enter"
                stage = "submission_unknown"
                try:
                    delivery_command(after, f"send-keys -t {target} {submit}")
                    break
                except DeliveryRefused as error:
                    # A false in-server condition proves no key was sent. Retry
                    # a redraw within this attempt, never an ambiguous write.
                    stage = "pasted"
                    if not error.transient or time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)
            try:
                delivery_tmux('set-option', '-puq', '-t', source_context()['pane'], '@kibitz-delivery-error')
            except (OSError, subprocess.SubprocessError):
                pass
    except Exception as error:
        raise DeliveryRefused(str(error), stage=stage) from error
    finally:
        if buffer:
            try:
                delivery_tmux("delete-buffer", "-b", buffer)
            except (OSError, subprocess.SubprocessError):
                pass


def delivery_failure_action(error):
    # Unknown exceptions are not proof of non-delivery.
    if isinstance(error, DeliveryRefused):
        if error.stage == 'not_pasted':
            return 'undelivered'
        if error.stage == 'pasted':
            return 'unsubmitted'
    return 'fail'


def report_delivery_failure(reason):
    stage = getattr(reason, "stage", "not_pasted" if isinstance(reason, str) else "submission_unknown")
    if stage == "not_pasted":
        detail = "not sent: " + str(reason) + "."
    elif stage == "pasted":
        detail = "text pasted, but no submit key sent: " + str(reason) + ". Check the target pane before retrying."
    else:
        detail = "delivery unconfirmed: " + str(reason) + ". Check the target pane before retrying; it may already have the message."
    message = "Kibitz " + detail
    log(message)
    try:
        path = private_runtime() / 'delivery-errors.log'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + message + '\n')
    except (OSError, DeliveryRefused):
        pass
    try:
        source = source_context()["pane"]
        delivery_tmux("set-option", "-pq", "-t", source, "@kibitz-delivery-error", message,
                      ";", "display-message", "-l", "-d", "5000", "-t", source, message)
    except (DeliveryRefused, OSError, subprocess.SubprocessError):
        print(message, file=sys.stderr)
    return message


def forward(target, payload, force=False, before_paste=None, expected_agent=None):
    return forward_prompt(target, payload, expected_agent=expected_agent,
                          force=force, before_paste=before_paste)


def main():
    """CLI sends use stdin so long messages never become command arguments."""
    try:
        source_context()
        if sys.argv[1:] == ["validate-source"]:
            return 0
        direction = sys.argv[1] if len(sys.argv) > 1 else ""
        pair = resolve_counterpart(None)
        if not pair or (direction == "host") == pair[2] or direction not in ("host", "reviewer"):
            raise DeliveryRefused("no live paired " + direction + " pane in this window")
        target, expected, _ = pair
        label = current_pane_label()
        agent = "codex" if label == "codex" else "claude"
        text = sys.stdin.read()
        if not text.strip():
            raise DeliveryRefused("empty message")
        forward_prompt(target, f"[kibitz from:{agent}]\n\n{text}", expected_agent=expected,
                       force="--force" in sys.argv[2:])
        return 0
    except Exception as error:
        print(report_delivery_failure(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
