"""Explicit /tee prompts work from either side of a Kibitz pair."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch, ANY

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
import kibitz_codex_stop as codex
import kibitz_hook_common as common
import kibitz_hook_user_prompt_submit as prompt


class TeeTest(unittest.TestCase):
    def setUp(self):
        import kibitz_hook_common as common
        context = dict(pane="%7", pane_pid="10", tty="/dev/pts/1", server="fixture,1",
                       ancestors={11: dict(command="codex", started="new", parent=10)})
        source_patch = patch.object(common, "source_context", return_value=context)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        self.peer = self.mock(prompt, "resolve_counterpart", return_value=("%1", None, False))
        self.forward = self.mock(prompt, "forward_prompt", side_effect=lambda *args, **kw:
            kw["before_paste"]() if kw.get("before_paste") else None)
        self.notify = self.mock(prompt, "notify")
        self.remember = self.mock(prompt, "remember_prompt")
        self.mock(prompt, "current_pane_label", return_value="codex")
        self.reviewer = self.mock(prompt, "resolve_reviewer", return_value=None)
        self.log = self.mock(prompt, "report_delivery_failure")
        self.rollout = self.mock(codex, "record_rollout")
        self.mock(codex, "log")

    def mock(self, owner, name, **kwargs):
        replacement = patch.object(owner, name, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def submit(self, text, agent="codex", **extra):
        payload = dict(hook_event_name="UserPromptSubmit", session_id="session",
                       transcript_path="/fixture/rollout.jsonl", prompt=text)
        payload["turn_id" if agent == "codex" else "prompt_id"] = "turn"
        payload.update(extra)
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual((codex if agent == "codex" else prompt).main(), 0)

    def test_codex_prompt_hook_is_registered(self):
        settings = json.loads((ROOT / "hooks/kibitz_codex_hooks.json").read_text())
        commands = [h["command"] for g in settings.get("UserPromptSubmit", []) for h in g["hooks"]]
        self.assertEqual(commands, ["$HOME/.local/bin/kibitz_codex_stop.py"])

    def test_codex_reviewer_sends_only_prompt_to_claude_host(self):
        self.submit("Think independently\nabout this /tee  ")
        self.forward.assert_called_once_with("%1", "[kibitz from:codex]\n\nUSER:\nThink independently\nabout this", expected_agent=None, before_paste=None)
        self.notify.assert_not_called()
        self.remember.assert_not_called()

    def test_codex_host_tracks_tee_using_its_turn_id(self):
        self.peer.return_value = ("%9", "claude", True)
        self.submit("Question /tee")
        self.forward.assert_called_once_with("%9", "[kibitz from:codex]\n\nUSER:\nQuestion", expected_agent="claude", before_paste=ANY)
        self.assertEqual([call.args[0] for call in self.notify.call_args_list], ["expect", "queue"])
        self.assertTrue(all(call.args[1]["prompt_id"] == "turn" for call in self.notify.call_args_list))
        self.remember.assert_not_called()

    def test_claude_reviewer_can_tee_back_to_codex(self):
        self.peer.return_value = ("%1", None, False)
        self.submit("Question /tee", agent="claude")
        self.forward.assert_called_once_with("%1", "[kibitz from:claude]\n\nUSER:\nQuestion", expected_agent=None, before_paste=None)
        self.notify.assert_not_called()
        self.remember.assert_not_called()

    def test_claude_host_keeps_prompt_capture_and_handoff(self):
        self.peer.return_value = ("%9", "codex", True)
        self.submit("Question /tee", agent="claude")
        self.forward.assert_called_once_with("%9", "[kibitz from:claude]\n\nUSER:\nQuestion", expected_agent="codex", before_paste=ANY)
        self.remember.assert_called_once()
        self.assertEqual([call.args[0] for call in self.notify.call_args_list], ["expect", "queue"])

    def test_codex_normal_muted_bare_and_nontrailing_directives_are_local(self):
        for text in ("Hello", "Hello /mute", "/tee", " /tee\n", "Hello /TEE", "Hello /tee later", "path/tee"):
            with self.subTest(text=text):
                self.submit(text)
        self.forward.assert_not_called()
        self.peer.assert_not_called()
        self.notify.assert_not_called()
        self.remember.assert_not_called()

    def test_relayed_messages_are_never_teed_back(self):
        for agent in ("claude", "codex"):
            self.submit("  [kibitz from:claude]\n\nUSER:\nQuestion /tee", agent=agent)
        self.forward.assert_not_called()
        self.peer.assert_not_called()

    def test_codex_internal_thread_cannot_forward_or_replace_rollout(self):
        self.submit("Question /tee", transcript_path=None)
        self.forward.assert_not_called()
        self.rollout.assert_not_called()
        self.peer.assert_not_called()

    def test_missing_pair_does_not_send_or_reserve(self):
        self.peer.return_value = None
        self.submit("Question /tee")
        self.forward.assert_not_called()
        self.notify.assert_not_called()

    def test_transport_failure_is_logged_without_blocking_prompt(self):
        self.forward.side_effect = subprocess.TimeoutExpired("tmux-bridge", 10)
        self.peer.return_value = ("%9", "claude", True)
        self.submit("Question /tee")
        self.log.assert_called_once()
        self.assertEqual([call.args[0] for call in self.notify.call_args_list], ["fail"])

    def test_task_wakeup_is_not_a_tee_prompt(self):
        for agent in ("claude", "codex"):
            self.submit("<task-notification>finished</task-notification> /tee", agent=agent)
        self.forward.assert_not_called()


class CounterpartTest(unittest.TestCase):
    rows = ("%1\t\t\t0\n%2\tcodex\t%1\t0\n"
            "%3\t\t\t0\n%4\tcodex\t%3\t0\n")

    def resolve(self, source, agent="codex", rows=None):
        with patch.dict(os.environ, TMUX_PANE=source), patch.object(common.subprocess, "run") as run, \
             patch.object(common, "valid_source", return_value=True):
            run.return_value = SimpleNamespace(stdout=self.rows if rows is None else rows)
            result = common.resolve_counterpart(agent)
            if source:
                self.assertEqual(run.call_args.args[0][:4], ["tmux", "list-panes", "-t", source])
            else:
                run.assert_not_called()
            return result

    def test_each_reviewer_uses_its_own_host_pointer(self):
        self.assertEqual(self.resolve("%2"), ("%1", None, False))
        self.assertEqual(self.resolve("%4"), ("%3", None, False))

    def test_each_host_uses_its_own_reviewer(self):
        self.assertEqual(self.resolve("%1", "claude"), ("%2", "codex", True))
        self.assertEqual(self.resolve("%3", "claude"), ("%4", "codex", True))

    def test_claude_reviewer_and_codex_host(self):
        rows = "%1\t\t\t0\n%2\tclaude-reviewer\t%1\t0\n"
        self.assertEqual(self.resolve("%1", rows=rows), ("%2", "claude", True))
        self.assertEqual(self.resolve("%2", "claude", rows), ("%1", None, False))

    def test_missing_closed_and_self_pointing_panes_do_not_route(self):
        for rows in ("%2\tcodex\t%1\t0\n", "%1\t\t\t1\n%2\tcodex\t%1\t0\n",
                     "%2\tcodex\t%2\t0\n", "%1\t\t\t0\n%2\tcodex\t%1\t1\n"):
            with self.subTest(rows=rows):
                self.assertIsNone(self.resolve("%2", rows=rows))
        self.assertIsNone(self.resolve(""))
        self.assertIsNone(self.resolve("%99"))

    def test_ambiguous_reviewers_do_not_route(self):
        rows = self.rows + "%5\tcodex\t%1\t0\n"
        self.assertIsNone(self.resolve("%1", "claude", rows))


if __name__ == "__main__":
    unittest.main()
