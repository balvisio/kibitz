"""Install wiring and real tmux transport, using isolated settings and panes."""
import json
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = Path(os.environ.get("KIBITZ_TEST_CLI") or ROOT / "kibitz")


def isolated_environment(home):
    env = dict(os.environ, HOME=str(home), XDG_CACHE_HOME=str(home / "cache"),
               CODEX_HOME=str(home / ".codex"), PYTHONDONTWRITEBYTECODE="1",
               TMUX_AGENT_INDICATOR_DIR=str(home / "no-indicator"), SHELL="/bin/bash")
    for name in ("TMUX", "TMUX_PANE", "TMUX_BRIDGE_SOCKET", "CLAUDE_CONFIG_DIR"):
        env.pop(name, None)
    return env


class TeeInstallTest(unittest.TestCase):
    def test_install_twice_then_uninstall_preserves_other_hooks(self):
        with tempfile.TemporaryDirectory(prefix="kibitz-tee-install-") as directory:
            home = Path(directory)
            env = isolated_environment(home)
            other = {"hooks": [{"type": "command", "command": "/example/other-hook"}]}
            settings = home / ".codex/hooks.json"
            settings.parent.mkdir()
            settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [other]}}))
            for attempt in range(2):
                result = subprocess.run(["bash", str(ROOT / "kibitz"), "install"], env=env,
                                        capture_output=True, text=True, check=True, timeout=30)
                self.assertEqual("Added Codex /tee hook" in result.stdout, attempt == 0)
            groups = json.loads(settings.read_text())["hooks"]["UserPromptSubmit"]
            self.assertEqual(groups, [other, {"hooks": [{"type": "command",
                "command": "$HOME/.local/bin/kibitz_codex_stop.py"}]}])
            self.assertTrue((home / ".local/bin/kibitz_hook_user_prompt_submit.py").is_file())
            subprocess.run(["bash", str(ROOT / "kibitz"), "uninstall"], env=env,
                           capture_output=True, text=True, check=True, timeout=30)
            self.assertEqual(json.loads(settings.read_text()), {"hooks": {"UserPromptSubmit": [other]}})


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class TeeTransportTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kibitz-tee-transport-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = isolated_environment(self.home)
        self.socket = self.home / "tmux.sock"
        self.tmux = [shutil.which("tmux"), "-S", str(self.socket)]
        self.addCleanup(lambda: subprocess.run(self.tmux + ["kill-server"], env=self.env,
                                               capture_output=True, timeout=10))
        self.receiver = self.home / "receiver.py"
        self.receiver.write_text('import sys\nsys.path.insert(0, ' + repr(str(ROOT / 'hooks')) + ')\n' + '''import json, os, re, signal, subprocess, sys, tty, time
from pathlib import Path
output, agent, status, after_paste, footer = Path(sys.argv[1]), *sys.argv[2:]
native = Path(os.environ["HOME"], ".claude/sessions", str(os.getpid()) + ".json")
def native_status(status):
    if agent != "claude": return
    domain = sys.platform
    if sys.platform == "linux":
        domain = "linux:" + Path("/etc/machine-id").read_text().strip() + ":" + os.readlink("/proc/self/ns/pid")
        started = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
    else:
        started = __import__("kibitz_processes").process(os.getpid())["started"]
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_text(json.dumps(dict(pid=os.getpid(), kind="interactive", pidDomain=domain,
        procStart=started, tmux="fixture."+os.environ["TMUX_PANE"], status=status)))
def draw(text):
    marker = "❯" if agent == "claude" else "›"
    busy = "Working (1s • esc to interrupt)\\r\\n" if status == "busy" else ""
    content = busy + marker + ("\u00a0" if agent == "claude" else " ") + text
    lines = content.split("\\r\\n")
    position = f"\\x1b[{len(lines)};{len(lines[-1])+1}H"
    paint = "\\x1b[48;5;236m" if agent == "codex" else ""
    os.write(1, ("\\x1b[2J\\x1b[H" + paint + content + "\\x1b[49m\\r\\n" + footer + position).encode())
def invoke(signum, frame):
    request = json.loads(output.with_suffix(".request").read_text())
    result = subprocess.run(request["command"], input=request.get("input", ""),
        env=dict(os.environ, **request.get("environment", {})), text=True, capture_output=True, timeout=25)
    output.with_suffix(".result").write_text(json.dumps(dict(
        returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)))
def question_footer(signum, frame):
    # Change only the footer of an empty idle composer. Keep its cursor,
    # prompt marker, geometry and terminal mode identical to the snapshot.
    native_status("waiting")
    os.write(1, "\\x1b[2;1H\\x1b[2KEnter to confirm · Esc to cancel\\x1b[1;3H".encode())
signal.signal(signal.SIGUSR1, invoke)
signal.signal(signal.SIGUSR2, question_footer)
tty.setraw(0)
os.write(1, b"\\x1b[?2004h")
native_status(status)
draw("1. Yes" if status == "waiting" else "" if agent == "claude" else "Ask anything")
output.with_suffix(".ready").touch()
buffer, records, pending = b"", [], None
pattern = re.compile(rb"\\x1b\\[200~(.*?)\\x1b\\[201~", re.S)
while True:
    data = os.read(0, 65536)
    with output.with_suffix(".bytes").open("ab") as stream: stream.write(data)
    buffer += data
    if (match := pattern.search(buffer)):
        pending = match[1].decode().replace("\\r", "\\n")
        buffer = buffer[match.end():]
        if after_paste == "shell":
            os.write(1, b"\\x1b[?2004l")
            os.execv("/bin/bash", ["bash", "--noprofile", "--norc", "-i"])
        if after_paste == "waiting":
            status = "waiting"
            native_status(status)
            draw("1. Yes")
        else:
            draw(pending.replace("\\n", "\\r\\n  ") if after_paste in ("tall", "flash") else "[Pasted text]")
        if after_paste == "flash":
            os.write(1, b"\\x1b[?25l")
            output.with_suffix(".hidden").touch()
            time.sleep(.5)
            os.write(1, b"\\x1b[?25h")
    if pending is not None and buffer[:1] in (b"\\r", b"\\t"):
        if status == "waiting": output.with_suffix(".approved").touch()
        records.append({"text": pending, "key": buffer[:1].decode()})
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(records))
        temporary.replace(output)
        buffer, pending = buffer[1:], None
        draw("" if agent == "claude" else "Ask anything")
''')
        bins = self.home / "bin"
        bins.mkdir()
        # The copied interpreters are fixture executables with the agents'
        # process names. No real model session or live pane receives test input.
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            # Homebrew's python executable is a launcher that execs Python.app.
            import ctypes
            buffer = ctypes.create_string_buffer(4096)
            self.assertGreater(ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidpath(os.getpid(), buffer, len(buffer)), 0)
            executable = Path(os.fsdecode(buffer.value))
        for agent in ("claude", "codex"):
            shutil.copy2(executable, bins / agent)
        bridge = bins / "tmux-bridge"
        # Isolate the vendored bridge's read-guard files as well as its socket;
        # fixture pane IDs must not consume guards belonging to a live server.
        bridge.write_text((ROOT / "scripts/tmux-bridge").read_text().replace(
            'echo "/tmp/tmux-bridge-read-', f'echo "{self.home}/tmux-bridge-read-'))
        bridge.chmod(0o755)
        self.env["PATH"] = str(bins) + os.pathsep + self.env["PATH"]
        self.outputs = {}

    def tmx(self, *args):
        return subprocess.check_output(self.tmux + list(args), env=self.env,
                                       text=True, stderr=subprocess.PIPE, timeout=10).strip()

    def pane(self, *command, agent, status="idle", after_paste="", footer="? for shortcuts"):
        output = self.home / f"received-{len(self.outputs)}.json"
        program = shlex.join([str(self.home / "bin" / agent), str(self.receiver), str(output),
                              agent, status, after_paste, footer])
        pane = self.tmx(*command, "-P", "-F", "#{pane_id}", "-c", str(self.home), program)
        self.outputs[pane] = output
        self.wait_for(lambda: output.with_suffix(".ready").exists())
        return pane

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.02)
        log = self.home / ".claude/kibitz-hook.log"
        self.fail("fixture did not receive the expected event: " + (log.read_text() if log.exists() else "no hook log"))

    def received(self, pane):
        path = self.outputs[pane]
        return json.loads(path.read_text()) if path.exists() else []

    def submit(self, pane, agent, text):
        payload = dict(hook_event_name="UserPromptSubmit", session_id="session-" + pane[1:],
                       transcript_path=str(self.home / (pane[1:] + ".jsonl")), prompt=text)
        payload["turn_id" if agent == "codex" else "prompt_id"] = "turn"
        name = "kibitz_codex_stop.py" if agent == "codex" else "kibitz_hook_user_prompt_submit.py"
        result = self.invoke(pane, [sys.executable, "-B", str(ROOT / "hooks" / name)], json.dumps(payload))
        self.assertEqual((result["returncode"], result["stdout"], result["stderr"]), (0, "", ""))

    def invoke(self, pane, command, input="", environment=None):
        output = self.outputs[pane]
        response = output.with_suffix(".result")
        if response.exists():
            response.unlink()
        output.with_suffix(".request").write_text(json.dumps(dict(
            command=command, input=input, environment=environment or {})))
        os.kill(int(self.tmx("display-message", "-p", "-t", pane, "#{pane_pid}")), signal.SIGUSR1)
        self.wait_for(response.exists)
        return json.loads(response.read_text())

    def test_both_agents_tee_from_host_and_reviewer_without_crossing_pairs(self):
        claude_host = self.pane("-f", "/dev/null", "new-session", "-d", "-s", "tee", "-x", "120", "-y", "40", agent="claude")
        codex_reviewer = self.pane("split-window", "-h", "-t", claude_host, agent="codex", status="busy")
        codex_host = self.pane("new-window", "-d", "-t", "tee", "-n", "second", agent="codex")
        claude_reviewer = self.pane("split-window", "-h", "-t", codex_host, agent="claude")
        for reviewer, host, label in ((codex_reviewer, claude_host, "codex"),
                                      (claude_reviewer, codex_host, "claude-reviewer")):
            self.tmx("set-option", "-p", "-t", reviewer, "@name", label)
            self.tmx("set-option", "-p", "-t", reviewer, "@kibitz_host_pane", host)
        self.tmx("select-window", "-t", codex_host)

        self.submit(codex_reviewer, "codex", "Literal $HOME and `text`\nsecond line /tee")
        self.wait_for(lambda: len(self.received(claude_host)) == 1)
        self.assertEqual(self.received(claude_host), [{"text":
            "[kibitz from:codex]\n\nUSER:\nLiteral $HOME and `text`\nsecond line", "key": "\r"}])
        self.assertFalse(self.received(claude_reviewer))

        # The working composer supplies live busy evidence even with a stale
        # cache. Claude must receive Enter regardless of an old Codex cache.
        cache = self.home / "cache/kibitz"
        cache.mkdir(parents=True, exist_ok=True)
        rollout = self.home / "busy.jsonl"
        rollout.write_text(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        for target in (codex_reviewer, claude_reviewer):
            (cache / f"pane-{target[1:]}.rollout").write_text(str(rollout))
        self.submit(claude_host, "claude", "Second question /tee")
        self.wait_for(lambda: len(self.received(codex_reviewer)) == 1)
        self.assertEqual(self.received(codex_reviewer), [{"text":
            "[kibitz from:claude]\n\nUSER:\nSecond question", "key": "\t"}])

        self.submit(codex_host, "codex", "Third question /tee")
        self.wait_for(lambda: len(self.received(claude_reviewer)) == 1)
        self.assertEqual(self.received(claude_reviewer), [{"text":
            "[kibitz from:codex]\n\nUSER:\nThird question", "key": "\r"}])
        self.submit(claude_reviewer, "claude", "Fourth question /tee")
        self.wait_for(lambda: len(self.received(codex_host)) == 1)
        self.assertEqual(self.received(codex_host), [{"text":
            "[kibitz from:claude]\n\nUSER:\nFourth question", "key": "\r"}])

        for pane, agent in ((codex_reviewer, "codex"), (claude_host, "claude")):
            for text in ("Ordinary question /mute", "/tee", "[kibitz from:codex]\n\nUSER:\nQuoted /tee"):
                self.submit(pane, agent, text)
        self.assertTrue(all(len(self.received(pane)) == 1 for pane in self.outputs))

    def pair(self, **options):
        host = self.pane("-f", "/dev/null", "new-session", "-d", "-s", "tee", "-x", "120", "-y", "40",
                         agent="claude", **options)
        reviewer = self.pane("split-window", "-h", "-t", host, agent="codex")
        self.tmx("set-option", "-p", "-t", reviewer, "@name", "codex")
        self.tmx("set-option", "-p", "-t", reviewer, "@kibitz_host_pane", host)
        return host, reviewer

    def test_tall_paste_and_existing_draft_are_submitted(self):
        host, reviewer = self.pair(after_paste="tall")
        text = "first line\n" + "continuation\n" * 12 + "last line /tee"
        self.submit(reviewer, "codex", text)
        self.wait_for(lambda: bool(self.received(host)))
        self.assertEqual(self.received(host)[0]["key"], "\r")
        self.assertIn("last line", self.received(host)[0]["text"])

    def assert_empty_claude_accepts_once(self, status):
        host, reviewer = self.pair(status=status)
        self.assertIn('❯\u00a0\n', self.tmx('capture-pane', '-p', '-t', host))
        self.submit(reviewer, 'codex', 'Empty composer regression /tee')
        self.wait_for(lambda: bool(self.received(host)))
        self.assertEqual(self.received(host), [{'text':
            '[kibitz from:codex]\n\nUSER:\nEmpty composer regression', 'key': '\r'}])
        raw = self.outputs[host].with_suffix('.bytes').read_bytes()
        self.assertEqual(raw.count(b'\x1b[200~'), 1)
        self.assertEqual(raw.split(b'\x1b[201~', 1)[1], b'\r')

    def test_empty_idle_claude_prompt_accepts_one_delivery(self):
        self.assert_empty_claude_accepts_once('idle')

    def test_empty_busy_claude_prompt_accepts_one_delivery(self):
        self.assert_empty_claude_accepts_once('busy')

    def test_claude_hyperlink_footer_accepts_one_delivery(self):
        footer = ('? for shortcuts · PR \x1b]8;id=pr;https://example.invalid/pr/12\x1b\\'
                  '#12\x1b]8;;\x1b\\ · \x1b]8;;https://example.invalid/main\x07main\x1b]8;;\x07')
        host, reviewer = self.pair(footer=footer)
        self.assertIn('\x1b]8;', self.tmx('capture-pane', '-e', '-p', '-t', host))
        self.submit(reviewer, 'codex', 'Linked footer regression /tee')
        self.wait_for(lambda: bool(self.received(host)))
        self.assertEqual(self.received(host), [{'text':
            '[kibitz from:codex]\n\nUSER:\nLinked footer regression', 'key': '\r'}])

    def assert_changed_question_footer_refused(self, **options):
        host, reviewer = self.pair(**options)
        script = """import os, signal, sys, time
sys.path.insert(0, sys.argv[1])
import kibitz_hook_common as common
pane = sys.argv[2]
original = common.delivery_target(pane, 'claude')
os.kill(int(original['pane_pid']), signal.SIGUSR2)
deadline = time.monotonic() + 3
while 'Enter to confirm' not in common.delivery_tmux('capture-pane', '-p', '-t', pane):
    if time.monotonic() >= deadline:
        raise AssertionError('fixture did not show its question')
    time.sleep(.01)
cursor = common.delivery_tmux('display-message', '-p', '-t', pane, '#{cursor_x},#{cursor_y}').strip()
assert cursor == original['cursor_x'] + ',' + original['cursor_y'], cursor
try:
    common.delivery_command(original, 'send-keys -t ' + pane + ' Enter')
except common.DeliveryRefused:
    pass
else:
    raise AssertionError('native guard accepted a changed question footer')
"""
        result = self.invoke(reviewer, [sys.executable, '-B', '-c', script, str(ROOT / 'hooks'), host])
        self.assertEqual(result['returncode'], 0, result)
        self.assertFalse(self.outputs[host].with_suffix('.bytes').exists())

    def test_empty_claude_prompt_still_rechecks_a_changed_question_footer(self):
        self.assert_changed_question_footer_refused()

    def test_hyperlink_footer_still_rechecks_a_changed_question(self):
        self.assert_changed_question_footer_refused(
            footer='? for shortcuts · \x1b]8;;https://example.invalid/pr/12\x1b\\#12\x1b]8;;\x1b\\')


    def test_busy_claude_redraw_after_paste_is_retried_once(self):
        host, reviewer = self.pair(status='busy', after_paste='flash')
        self.submit(reviewer, 'codex', 'One message /tee')
        self.wait_for(lambda: bool(self.received(host)))
        self.assertTrue(self.outputs[host].with_suffix('.hidden').exists())
        raw = self.outputs[host].with_suffix('.bytes').read_bytes()
        self.assertEqual(raw.count(b'\x1b[200~'), 1)
        self.assertEqual(len(self.received(host)), 1)

    def test_rule_and_numbered_lines_are_content_in_codex(self):
        host = self.pane('-f', '/dev/null', 'new-session', '-d', '-s', 'tee', '-x', '120', '-y', '40', agent='claude')
        reviewer = self.pane('split-window', '-h', '-t', host, agent='codex', after_paste='tall')
        self.tmx('set-option', '-p', '-t', reviewer, '@name', 'codex')
        self.tmx('set-option', '-p', '-t', reviewer, '@kibitz_host_pane', host)
        self.submit(host, 'claude', 'Example:\n─────\n› 1. This is text\nlast /tee')
        self.wait_for(lambda: bool(self.received(reviewer)))
        self.assertEqual(len(self.received(reviewer)), 1)

    def test_codex_tall_message_is_submitted(self):
        host = self.pane("-f", "/dev/null", "new-session", "-d", "-s", "tee", "-x", "120", "-y", "40",
                         agent="claude")
        reviewer = self.pane("split-window", "-h", "-t", host, agent="codex", after_paste="tall")
        self.tmx("set-option", "-p", "-t", reviewer, "@name", "codex")
        self.tmx("set-option", "-p", "-t", reviewer, "@kibitz_host_pane", host)
        text = "first line\n" + "continuation\n" * 12 + "last line /tee"
        self.submit(host, "claude", text)
        self.wait_for(lambda: bool(self.received(reviewer)))
        self.assertEqual(self.received(reviewer)[0]["key"], "\r")
        self.assertIn("last line", self.received(reviewer)[0]["text"])

    def test_native_guard_rechecks_identity_cursor_geometry_and_mode(self):
        host, reviewer = self.pair()
        script = """import sys
sys.path.insert(0, sys.argv[1])
import kibitz_hook_common as common
pane = sys.argv[2]
original = common.delivery_target(pane)
for field, wrong in (("pane_pid", "999999"), ("command", "bash"),
                     ("cursor_x", "79"), ("cursor_y", "39"),
                     ("width", "999"), ("height", "999")):
    try:
        common.delivery_command(dict(original, **{field: wrong}), "send-keys -t " + pane + " Enter")
    except common.DeliveryRefused:
        continue
    raise AssertionError("native guard did not check " + field)
common.delivery_tmux("copy-mode", "-t", pane)
try:
    common.delivery_command(original, "send-keys -t " + pane + " Enter")
except common.DeliveryRefused:
    pass
else:
    raise AssertionError("native guard did not check mode")
"""
        result = self.invoke(reviewer, [sys.executable, "-c", script, str(ROOT / "hooks"), host])
        self.assertEqual(result["returncode"], 0, result)
        self.assertFalse(self.outputs[host].with_suffix(".bytes").exists())


    def test_native_guard_checks_text_even_when_cursor_does_not_move(self):
        host, reviewer = self.pair()
        script = """import sys
sys.path.insert(0, sys.argv[1])
import kibitz_hook_common as common
pane = sys.argv[2]
original = common.delivery_target(pane)
for text in ('❯ Different text', '? Different footer'):
    changed = dict(original)
    lines = original['screen'].splitlines()
    row = original['composer_row'] if text.startswith('❯') else int(original['cursor_y']) + 1
    lines[row] = text
    changed['screen'] = '\\n'.join(lines)
    try:
        common.delivery_command(changed, 'send-keys -t ' + pane + ' Enter')
    except common.DeliveryRefused:
        continue
    raise AssertionError('native guard accepted stale text')
"""
        result = self.invoke(reviewer, [sys.executable, '-c', script, str(ROOT / 'hooks'), host])
        self.assertEqual(result['returncode'], 0, result)
        self.assertFalse(self.outputs[host].with_suffix('.bytes').exists())

    def test_claude_background_shell_state_allows_delivery(self):
        host, reviewer = self.pair(status="shell")
        self.submit(reviewer, "codex", "Question /tee")
        self.wait_for(lambda: bool(self.received(host)))

    def test_source_spoofing_cannot_forward_or_change_another_panes_rollout(self):
        host, reviewer = self.pair()
        payload = json.dumps(dict(hook_event_name="UserPromptSubmit", session_id="spoof",
                            transcript_path="/spoof.jsonl", prompt="Do not deliver /tee"))
        result = self.invoke(reviewer, [sys.executable, "-B", str(ROOT / "hooks/kibitz_codex_stop.py")],
                             payload, environment={"TMUX_PANE": host})
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(self.received(host))
        self.assertFalse(self.received(reviewer))
        self.assertFalse((self.home / f"cache/kibitz/pane-{host[1:]}.rollout").exists())

    def test_manual_send_and_relay_refuse_a_question_even_with_force(self):
        host, reviewer = self.pair(status="waiting")
        result = self.invoke(reviewer, ["bash", str(CLI), "send", "Never approve"])
        self.assertEqual(result["returncode"], 1, result)
        self.assertIn("not sent", result["stderr"])
        cache = self.home / "cache/kibitz"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "codex-fixture.msg").write_text(json.dumps(dict(turn_id="turn", message="Never approve")))
        result = self.invoke(reviewer, ["bash", str(CLI), "relay", "--force"],
                             environment={"CODEX_THREAD_ID": "fixture"})
        self.assertEqual(result["returncode"], 1, result)
        self.assertFalse(self.outputs[host].with_suffix(".bytes").exists())

    def test_manual_send_to_shell_cannot_execute_text(self):
        host, reviewer = self.pair()
        self.tmx("respawn-pane", "-k", "-t", host, "/bin/bash --noprofile --norc -i")
        marker = self.home / "must-not-exist"
        result = self.invoke(reviewer, ["bash", str(CLI), "send", "touch " + str(marker)])
        self.assertEqual(result["returncode"], 1, result)
        self.assertFalse(marker.exists())

    def test_manual_send_and_relay_use_the_guarded_transport(self):
        host, reviewer = self.pair()
        result = self.invoke(reviewer, ["bash", str(CLI), "send", "literal $HOME #{pane_id}"])
        self.assertEqual(result["returncode"], 0, result)
        self.wait_for(lambda: bool(self.received(host)))
        self.assertEqual(self.received(host)[0]["text"], "[kibitz from:codex]\n\nliteral $HOME #{pane_id}")
        cache = self.home / "cache/kibitz"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "codex-fixture.msg").write_text(json.dumps(dict(turn_id="turn", message="My review")))
        result = self.invoke(reviewer, ["bash", str(CLI), "relay"],
                             environment={"CODEX_THREAD_ID": "fixture"})
        self.assertEqual(result["returncode"], 0, result)
        self.wait_for(lambda: len(self.received(host)) == 2)
        self.assertEqual(self.received(host)[1]["text"], "[kibitz from:codex]\n\nMy review")


    def test_manual_send_works_with_an_unwritable_home_cache(self):
        host, reviewer = self.pair()
        cache = self.home / 'readonly'
        cache.mkdir(mode=0o500)
        self.addCleanup(lambda: cache.chmod(0o700))
        result = self.invoke(reviewer, ['bash', str(CLI), 'send', 'from the sandbox'],
                             environment={'XDG_CACHE_HOME': str(cache)})
        self.assertEqual(result['returncode'], 0, result)
        self.wait_for(lambda: bool(self.received(host)))
        self.assertEqual(self.received(host)[0]['text'], '[kibitz from:codex]\n\nfrom the sandbox')

    def test_permission_dialog_receives_no_paste_or_enter(self):
        host, reviewer = self.pair(status="waiting")
        self.submit(reviewer, "codex", "Do not approve anything /tee")
        self.assertFalse(self.outputs[host].with_suffix(".bytes").exists())
        self.assertFalse(self.outputs[host].with_suffix(".approved").exists())

    def test_dialog_appearing_after_paste_receives_no_submit(self):
        host, reviewer = self.pair(after_paste="waiting")
        self.submit(reviewer, "codex", "Do not approve anything /tee")
        self.assertTrue(self.outputs[host].with_suffix(".bytes").read_bytes().endswith(b"\x1b[201~"))
        self.assertFalse(self.outputs[host].with_suffix(".approved").exists())
        self.assertFalse(self.received(host))

    def test_agent_replaced_by_shell_after_paste_receives_no_enter(self):
        host, reviewer = self.pair(after_paste="shell")
        self.submit(reviewer, "codex", "Do not execute anything /tee")
        self.assertTrue(self.outputs[host].with_suffix(".bytes").read_bytes().endswith(b"\x1b[201~"))
        self.assertFalse(self.received(host))

    def test_shell_prompt_cannot_execute_forwarded_text(self):
        host, reviewer = self.pair()
        self.tmx("respawn-pane", "-k", "-t", host, "/bin/bash --noprofile --norc -i")
        marker = self.home / "must-not-exist"
        self.submit(reviewer, "codex", "$(touch " + shlex.quote(str(marker)) + ")\n" +
                    "touch " + shlex.quote(str(marker)) + " /tee")
        self.assertFalse(marker.exists())
        self.assertNotIn("USER:", self.tmx("capture-pane", "-p", "-t", host))


if __name__ == "__main__":
    unittest.main()
