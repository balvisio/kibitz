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


def running(*task_ids):
    return [{"id": task_id, "type": "shell", "status": "running", "description": ""} for task_id in task_ids]


class StopHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.forwarded = []
        self.saved = {name: getattr(hook, name) for name in
                      ("forward", "resolve_reviewer", "current_pane_label", "LAST_FORWARD_PATH", "CACHE_DIR")}
        hook.forward = lambda pane, message: self.forwarded.append(message)
        hook.resolve_reviewer = lambda: ("%9", "codex")
        hook.current_pane_label = lambda: ""
        hook.LAST_FORWARD_PATH = root / "kibitz-last.txt"
        hook.CACHE_DIR = root / "cache"
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
        return [m.split("USER:\n", 1)[1].split("\n\nCLAUDE:\n", 1) for m in self.forwarded]

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

    def test_duplicate_stop_is_deduped(self):
        entries = [human("A"), assistant("a")]
        self.assertEqual(self.stop(entries, "a"), [["A", "a"]])
        self.assertEqual(self.stop(entries, "a"), [])


if __name__ == "__main__":
    unittest.main()
