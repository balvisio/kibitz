"""Regression tests for the Claude Code Stop hook. Run with
`python3 -m unittest discover -s tests` from the repository root."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
os.environ["TMUX_PANE"] = "%1"

import kibitz_hook_stop as hook  # noqa: E402


def human(text):
    return {"type": "user", "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}


def notification(task_id, status="completed"):
    body = f"<task-notification>\n<task-id>{task_id}</task-id>\n"
    if status:
        body += f"<status>{status}</status>\n"
    body += "<summary>x</summary>\n</task-notification>"
    return {"type": "user", "message": {"role": "user", "content": body}, "origin": {"kind": "task-notification"}}


def launch(task_id, key="backgroundTaskId"):
    content = [{"type": "tool_result", "tool_use_id": "toolu_x", "content": "Command running in background"}]
    return {"type": "user", "message": {"role": "user", "content": content}, "toolUseResult": {key: task_id}}


def assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def chain(entries):
    """Give entries sequential uuids with each parented to the previous one."""
    for i, entry in enumerate(entries):
        entry["uuid"] = f"u{i}"
        if i:
            entry["parentUuid"] = f"u{i - 1}"
    return entries


def running(*task_ids):
    return [{"id": task_id, "type": "shell", "status": "running", "description": ""} for task_id in task_ids]


class StopHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.forwarded = []
        self.saved = {name: getattr(hook, name) for name in
                      ("forward", "resolve_reviewer", "current_pane_label", "LAST_FORWARD_PATH")}
        hook.forward = lambda pane, message: self.forwarded.append(message)
        hook.resolve_reviewer = lambda: ("%9", "codex")
        hook.current_pane_label = lambda: ""
        hook.LAST_FORWARD_PATH = root / "kibitz-last.txt"
        self.transcript = root / "transcript.jsonl"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(hook, name, value)
        self.tmp.cleanup()

    def stop(self, entries, last_message, background_tasks=()):
        with self.transcript.open("w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        payload = {
            "session_id": "s",
            "transcript_path": str(self.transcript),
            "hook_event_name": "Stop",
            "stop_hook_active": False,
            "last_assistant_message": last_message,
        }
        if background_tasks is not None:
            payload["background_tasks"] = list(background_tasks)
        self.forwarded.clear()
        sys.stdin = io.StringIO(json.dumps(payload))
        self.assertEqual(hook.main(), 0)
        out = []
        for m in self.forwarded:
            body = m.split("\n\n", 1)[1]
            if body.startswith("USER:\n"):
                out.append(body[len("USER:\n"):].split("\n\nCLAUDE:\n", 1))
            else:
                out.append(body)
        return out

    def test_plain_exchange_is_forwarded(self):
        self.assertEqual(self.stop([human("A"), assistant("a")], "a"), [["A", "a"]])

    def test_interim_stop_with_pending_task_is_suppressed(self):
        entries = [human("A"), launch("t1"), assistant("waiting")]
        self.assertEqual(self.stop(entries, "waiting", running("t1")), [])

    def test_final_stop_after_completion_is_attributed_to_prompt(self):
        entries = [human("A"), launch("t1"), assistant("waiting"), notification("t1", status=None),
                   assistant("still waiting")]
        self.assertEqual(self.stop(entries, "still waiting", running("t1")), [])
        entries += [notification("t1"), assistant("done")]
        self.assertEqual(self.stop(entries, "done"), [["A", "done"]])

    def test_unrelated_pending_task_does_not_swallow_final_answer(self):
        entries = [human("A"), launch("ta"), assistant("waiting"), human("B"), launch("tb"),
                   assistant("B waiting"), notification("ta"), assistant("A done")]
        self.assertEqual(self.stop(entries, "A done", running("tb")), [["A", "A done"]])

    def test_chained_tasks_keep_owner_and_honor_mute(self):
        def chain(first_prompt):
            return [human(first_prompt), launch("ta1"), assistant("waiting"), human("B"), assistant("b"),
                    notification("ta1"), launch("ta2"), assistant("second step"), notification("ta2"),
                    assistant("A done")]
        self.assertEqual(self.stop(chain("A /mute"), "A done"), [])
        self.assertEqual(self.stop(chain("A"), "A done"), [["A", "A done"]])

    def test_chained_interim_stop_is_suppressed(self):
        entries = [human("A"), launch("ta1"), assistant("waiting"), human("B"), assistant("b"),
                   notification("ta1"), launch("ta2"), assistant("second step")]
        self.assertEqual(self.stop(entries, "second step", running("ta2")), [])

    def test_unrelated_prompt_is_forwarded_while_other_task_runs(self):
        entries = [human("A"), launch("ta"), assistant("waiting"), human("B"), assistant("b")]
        self.assertEqual(self.stop(entries, "b", running("ta")), [["B", "b"]])

    def test_event_for_earlier_prompt_after_unrelated_prompt_is_suppressed(self):
        entries = [human("A"), launch("ta"), assistant("waiting"), human("B"), assistant("b"),
                   notification("ta", status=None), assistant("still waiting")]
        self.assertEqual(self.stop(entries, "still waiting", running("ta")), [])

    def test_agent_and_monitor_ids_are_recognized(self):
        for key in ("agentId", "taskId"):
            entries = [human("A"), launch("x", key), assistant("waiting")]
            self.assertEqual(self.stop(entries, "waiting", running("x")), [], key)

    def test_payload_without_background_tasks_forwards(self):
        entries = [human("A"), launch("t1"), assistant("waiting")]
        self.assertEqual(self.stop(entries, "waiting", background_tasks=None), [["A", "waiting"]])

    def test_untraceable_notification_is_not_forwarded(self):
        fork = {"type": "user", "origin": {"kind": "task-notification"},
                "message": {"role": "user", "content": "<task-notification>\n<fork-source>x</fork-source>\n</task-notification>"}}
        entries = [human("A"), assistant("a"), human("B"), assistant("b")]
        self.assertEqual(self.stop(entries + [fork, assistant("ack")], "ack"), [])
        self.assertEqual(self.stop(entries + [notification("unknown"), assistant("ack")], "ack"), [])

    def test_slash_command_output_is_not_a_prompt(self):
        artifacts = [human("<local-command-caveat>x</local-command-caveat>"),
                     human("<command-name>/effort</command-name>"),
                     human("<local-command-stdout>Set effort level to max</local-command-stdout>"),
                     human("<bash-input>kibitz start</bash-input>")]
        self.assertEqual(self.stop(artifacts + [assistant("hello")], "hello"), [])

    def test_shell_command_reply_is_not_forwarded(self):
        shell = [human("<bash-input>git status</bash-input>"), human("<bash-stdout>clean</bash-stdout>")]
        entries = [human("Fix this bug"), assistant("Fixed it")] + shell + [assistant("Tree is clean")]
        self.assertEqual(self.stop(entries, "Tree is clean"), [])
        entries += [human("now add a test"), assistant("Added")]
        self.assertEqual(self.stop(entries, "Added"), [["now add a test", "Added"]])

    def test_setting_changed_mid_turn_keeps_owner(self):
        tool_call = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest"}}]}}
        tool_result = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "1 passed"}]}, "toolUseResult": {"stdout": "1 passed"}}
        slash = [human("<local-command-caveat>x</local-command-caveat>"),
                 human("<command-name>/effort</command-name>"),
                 human("<local-command-stdout>Set effort level to max</local-command-stdout>")]
        entries = [human("Fix this bug"), tool_call] + slash + [tool_result, assistant("Fixed it. Tests pass.")]
        self.assertEqual(self.stop(entries, "Fixed it. Tests pass."), [["Fix this bug", "Fixed it. Tests pass."]])
        entries = [human("Fix this bug")] + slash + [launch("t1"), assistant("tests running")]
        self.assertEqual(self.stop(entries, "tests running", running("t1")), [])
        entries += [notification("t1"), assistant("Tests pass")]
        self.assertEqual(self.stop(entries, "Tests pass"), [["Fix this bug", "Tests pass"]])

    def test_slash_command_between_launch_and_completion_keeps_owner(self):
        slash = [human("<local-command-caveat>x</local-command-caveat>"),
                 human("<command-name>/effort</command-name>"),
                 human("<local-command-stdout>Set effort level to max</local-command-stdout>")]
        entries = [human("Fix this bug"), launch("t1"), assistant("tests running")] + slash + [
            notification("t1"), assistant("Tests pass")]
        self.assertEqual(self.stop(entries, "Tests pass"), [["Fix this bug", "Tests pass"]])

    @staticmethod
    def relay_output(note=""):
        return hook.RELAY_LINE + "\n" + (note + "\n" if note else "")

    def shell_relay(self, parent, note=""):
        tail = chain([human("<bash-input>kibitz relay note</bash-input>"),
                      human(f"<bash-stdout>{self.relay_output(note)}</bash-stdout><bash-stderr></bash-stderr>"),
                      assistant("Queued.")])
        for e in tail:
            e["uuid"] = "r-" + e["uuid"]
            e["parentUuid"] = "r-" + e["parentUuid"] if "parentUuid" in e else parent
        return tail

    def tool_relay(self, note=""):
        stdout = self.relay_output(note)
        return [assistant("Relaying now."),
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_r", "name": "Bash", "input": {"command": "kibitz relay"}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_r", "content": stdout}]},
                 "toolUseResult": {"stdout": stdout, "stderr": ""}},
                assistant("Sent.")]

    def test_relay_sends_reply_visible_when_command_ran(self):
        entries = chain([human("A"), assistant("R0"), human("Q2"), assistant("R1")])
        self.assertEqual(self.stop(entries + self.shell_relay("u1", "my note"), "Queued."),
                         ["CLAUDE:\nR0\n\nUSER:\nmy note"])

    def test_relay_without_note_is_verbatim(self):
        entries = chain([human("A"), assistant("R0")])
        self.assertEqual(self.stop(entries + self.shell_relay("u1"), "Queued."), ["R0"])

    def test_relay_from_tool_call_skips_claudes_own_turn(self):
        entries = chain([human("A"), assistant("R0"), human("relay that to codex")] + self.tool_relay("please check"))
        self.assertEqual(self.stop(entries, "Sent."),
                         ["CLAUDE:\nR0\n\nUSER:\nplease check", ["relay that to codex", "Sent."]])

    def test_relay_is_sent_once_and_not_replayed_by_later_stops(self):
        entries = chain([human("A"), assistant("R0")]) + self.shell_relay("u1", "note")
        self.assertEqual(self.stop(entries, "Queued."), ["CLAUDE:\nR0\n\nUSER:\nnote"])
        later = chain([human("B"), assistant("RB")])
        later[0]["parentUuid"] = "r-u2"
        self.assertEqual(self.stop(entries + later, "RB"), [["B", "RB"]])

    def test_later_shell_command_turn_does_not_resend_earlier_relay(self):
        entries = chain([human("Fix this bug"), assistant("I fixed the missing null check.")])
        entries += self.shell_relay("u1", "Is this fix correct?")
        self.assertEqual(self.stop(entries, "Queued."), ["CLAUDE:\nI fixed the missing null check.\n\nUSER:\nIs this fix correct?"])
        git = chain([human("<bash-input>git status</bash-input>"),
                     human("<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>"),
                     assistant("Your working tree is clean.")])
        for e in git:
            e["uuid"] = "g-" + e["uuid"]
            e["parentUuid"] = "g-" + e["parentUuid"] if "parentUuid" in e else "r-u2"
        self.assertEqual(self.stop(entries + git, "Your working tree is clean."), [])

    def test_prompt_quoting_the_marker_is_not_a_relay(self):
        quoted = "What does '[kibitz] relay queued' mean?"
        entries = chain([human("A"), assistant("R0"), human(quoted + " /mute"), assistant("It is the marker line.")])
        self.assertEqual(self.stop(entries, "It is the marker line."), [])
        entries = chain([human("A"), assistant("R0"), human(quoted), assistant("It is the marker line.")])
        self.assertEqual(self.stop(entries, "It is the marker line."), [[quoted, "It is the marker line."]])

    def test_marker_inside_other_command_output_is_not_a_relay(self):
        grep = f'RELAY_LINE = "{hook.RELAY_LINE}"'
        for stdout in (grep, "something first\n" + hook.RELAY_LINE):
            entries = chain([human("A"), assistant("R0"),
                             human("<bash-input>rg RELAY_LINE hooks</bash-input>"),
                             human(f"<bash-stdout>{stdout}\n</bash-stdout><bash-stderr></bash-stderr>"),
                             assistant("That is the marker constant.")])
            self.assertEqual(self.stop(entries, "That is the marker constant."), [], stdout)

    def test_relay_without_reviewer_is_dropped(self):
        entries = chain([human("A"), assistant("R0")]) + self.shell_relay("u1", "note")
        hook.resolve_reviewer = lambda: None
        self.assertEqual(self.stop(entries, "Queued."), [])
        hook.resolve_reviewer = lambda: ("%9", "codex")
        later = chain([human("B"), assistant("RB")])
        later[0]["parentUuid"] = "r-u2"
        self.assertEqual(self.stop(entries + later, "RB"), [["B", "RB"]])

    def test_relay_send_failure_is_logged_not_raised(self):
        entries = chain([human("A"), assistant("R0")]) + self.shell_relay("u1", "note")

        def boom(pane, message):
            raise RuntimeError("tmux-bridge down")
        hook.forward = boom
        self.assertEqual(self.stop(entries, "Queued."), [])

    def test_duplicate_stop_is_deduped(self):
        entries = [human("A"), assistant("a")]
        self.assertEqual(self.stop(entries, "a"), [["A", "a"]])
        self.assertEqual(self.stop(entries, "a"), [])


if __name__ == "__main__":
    unittest.main()
