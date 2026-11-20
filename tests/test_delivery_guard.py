"""Delivery requires a live foreground agent and its actual input composer."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import kibitz_hook_common as common


class ScreenRowsTest(unittest.TestCase):
    def test_hyperlink_metadata_is_not_visible_text_or_a_background_cell(self):
        for end in ('\x1b\\', '\x07'):
            with self.subTest(terminator=repr(end)):
                opening = '\x1b]8;id=review;https://example.invalid/#{pane_id},}' + end
                closing = '\x1b]8;;' + end
                screen = (opening + '\x1b[48;5;236m› draft\n  continued' + closing
                          + '\x1b[49m\nPR ' + opening + '\x1b[34m#12\x1b[39m'
                          + closing + ' · main\n')
                rows, backgrounds = common.screen_rows(screen)
                self.assertEqual(rows, ['› draft', '  continued', 'PR #12 · main'])
                self.assertEqual(backgrounds, [(5, 236), (5, 236), None])

    def test_adjacent_links_keep_their_labels_and_surrounding_text(self):
        screen = ('PR \x1b]8;;https://example.invalid/1\x1b\\#1\x1b]8;;\x1b\\'
                  ' and \x1b]8;id=second;https://example.invalid/2\x07#2\x1b]8;;\x07 · main')
        self.assertEqual(common.screen_rows(screen), (['PR #1 and #2 · main'], [None]))


class DeliveryStateTest(unittest.TestCase):
    def setUp(self):
        self.processes = {
            10: dict(parent=1, foreground=False, stopped=False, started="old", command="bash"),
            11: dict(parent=10, foreground=True, stopped=False, started="new", command="codex"),
        }
        self.statuses = {}
        self.text = "history\n\n› Ask anything\n? for shortcuts\n"
        self.mode, self.paste, self.visible, self.row, self.version = "0", "1", "1", 2, "3.7a"

    def observe(self, expected=None):
        header = f"10\t/dev/pts/1\tcodex\t0\t{self.mode}\t{self.row}\t{self.visible}\t{self.paste}\t{self.version}\t2\t80\t40\n"
        with patch.object(common, "delivery_tmux", return_value=header + self.text), \
             patch.object(common, "terminal_processes", return_value=self.processes), \
             patch.object(common, "claude_delivery_status", return_value=self.statuses):
            return common.delivery_target("%1", expected)

    def test_live_codex_composer_is_recognized(self):
        self.assertEqual(self.observe("codex")["agent"], "codex")

    def test_codex_permission_and_question_footers_are_rejected(self):
        for text in ("Command\n\n› 1. Yes\nEnter to confirm · Esc to cancel\n",
                     "Question\n\n› Answer here\nEnter to submit · Esc to interrupt\n"):
            with self.subTest(text=text):
                self.text = text
                with self.assertRaises(common.DeliveryRefused):
                    self.observe()

    def test_claude_native_waiting_overrides_an_old_normal_composer(self):
        self.processes[11]["command"] = "claude"
        self.statuses[11] = "waiting"
        self.text = "history\n\n❯ Ask anything\n? for shortcuts\n"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()
        for state in ("idle", "busy", "shell"):
            self.statuses[11] = state
            self.assertEqual(self.observe()["agent"], "claude")

    def test_background_unrelated_stopped_and_ambiguous_agents_are_rejected(self):
        for changes in (dict(foreground=False), dict(parent=99), dict(stopped=True)):
            with self.subTest(changes=changes), patch.dict(self.processes[11], changes):
                with self.assertRaises(common.DeliveryRefused):
                    self.observe()
        self.processes[12] = dict(self.processes[11])
        with self.assertRaises(common.DeliveryRefused):
            self.observe()

    def test_stale_claude_status_cannot_turn_a_shell_into_an_agent(self):
        self.processes[11]["command"] = "bash"
        self.statuses[11] = "idle"
        self.text = "history\n\n❯ Ask anything\n? for shortcuts\n"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()

    def test_copy_mode_missing_bracketed_paste_and_wrong_agent_are_rejected(self):
        with self.assertRaises(common.DeliveryRefused):
            self.observe("claude")
        self.mode = "1"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()
        self.mode, self.paste = "0", "0"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()

    def test_tall_composer_is_not_limited_to_eight_rows(self):
        self.text = "history\n\n› first line\n" + "  continued input\n" * 15 + "? for shortcuts\n"
        self.row = 17
        self.assertIn("continued input", self.observe()["composer"])

    def test_old_transcript_prompt_cannot_cross_composer_boundary(self):
        self.text = "› old transcript\n──────────────\nnotes input\n? for shortcuts\n"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()

    def test_invisible_cursor_and_numbered_choices_without_footer_are_rejected(self):
        self.visible = "0"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()
        self.visible = "1"
        self.text = "question\n\n› 1. Yes\n\n"
        with self.assertRaises(common.DeliveryRefused):
            self.observe()

    def test_requirement_names_the_server_version(self):
        self.version = "3.6a"
        with self.assertRaisesRegex(common.DeliveryRefused, "tmux 3.7.*3.6a"):
            self.observe()

    def test_missing_linux_machine_id_is_not_an_exception(self):
        with patch.object(common.sys, "platform", "linux"), \
             patch.object(Path, "read_text", side_effect=FileNotFoundError), \
             patch.object(common.os, "readlink", return_value="pid:[1]"):
            self.assertEqual(common.claude_delivery_status("%1", self.processes), {})

    def test_native_session_requires_matching_pane_domain_and_process_birth(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, CLAUDE_CONFIG_DIR=directory), \
             patch.object(common.sys, "platform", "darwin"):
            path = Path(directory) / "sessions/live.json"
            path.parent.mkdir()
            native = dict(pid=11, kind="interactive", pidDomain="darwin", procStart="new",
                          tmux="session:@1.%1", status="idle")
            path.write_text(json.dumps(native))
            self.assertEqual(common.claude_delivery_status("%1", self.processes), {11: "idle"})
            for changes in (dict(pid=99), dict(pidDomain="other"), dict(procStart="older"),
                            dict(tmux="session:@1.%2"), dict(kind="background")):
                path.write_text(json.dumps(dict(native, **changes)))
                self.assertFalse(common.claude_delivery_status("%1", self.processes))

    def test_notes_dialog_and_unmarked_feedback_are_not_composers(self):
        cases = [
            "Question 1/1\nChoose\n› Add notes\ntab or esc to clear notes | enter to submit answer\n",
            "Question 1/1\nChoose\n› Your answer\n⌃j to submit answer | esc to interrupt\n",
            "› old transcript\nFeedback for team\n  text here\nctrl+s to send\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.text = text
                with self.assertRaises(common.DeliveryRefused):
                    self.observe()


    def test_uncoloured_shell_mode_cannot_use_an_old_transcript_marker(self):
        self.text = '› old message\n\n! command input\n? for shortcuts\n'
        with self.assertRaises(common.DeliveryRefused):
            self.observe()

    def test_coloured_composer_accepts_ultra_marker_and_quoted_selections(self):
        self.text = "history\n\x1b[48;5;236m\n» existing draft\n  ───────\n  › 1. This is quoted text\n  last line\n\x1b[49m? for shortcuts\n"
        self.row = 5
        seen = self.observe()
        self.assertEqual(seen['composer_row'], 2)
        self.assertIn('quoted text', seen['composer'])


    def test_native_claude_version_binary_needs_a_matching_session_record(self):
        self.processes[11]['command'] = '2.1.325'
        self.text = 'history\n\n❯ Ask anything\n? for shortcuts\n'
        with self.assertRaises(common.DeliveryRefused):
            self.observe()
        self.statuses[11] = 'idle'
        self.assertEqual(self.observe('claude')['agent'], 'claude')

    def test_claude_foreground_mcp_child_does_not_replace_terminal_owner(self):
        self.processes[11]['command'] = 'claude'
        self.statuses[11] = 'busy'
        self.processes[12] = dict(self.processes[11], parent=11, command='codex')
        self.text = 'history\n\n❯ Ask anything\n? for shortcuts\n'
        self.assertEqual(self.observe('claude')['identity'][2], 11)

    def test_enter_to_confirm_footer_is_rejected_without_a_numbered_choice(self):
        self.text = 'history\n\n› input\nEnter to confirm\n'
        with self.assertRaisesRegex(common.DeliveryRefused, 'question'):
            self.observe()

    def test_hyperlinked_question_footer_still_refuses_both_agents(self):
        for agent, marker in (('claude', '❯'), ('codex', '›')):
            for end in ('\x1b\\', '\x07'):
                with self.subTest(agent=agent, terminator=repr(end)):
                    self.processes[11]['command'] = agent
                    self.statuses[11] = 'idle'
                    self.text = ('history\n\n' + marker + ' input\nEnter '
                                 '\x1b]8;;https://example.invalid/help' + end
                                 + 'to confirm\x1b]8;;' + end + '\n')
                    with self.assertRaisesRegex(common.DeliveryRefused, 'waiting on a question'):
                        self.observe(agent)


class DeliveryRaceTest(unittest.TestCase):
    def test_replaced_agent_never_receives_the_submit_key(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(common, "CACHE_DIR", Path(directory)), \
             patch.object(common, "delivery_tmux") as tmux, \
             patch.object(common, "delivery_command") as command, \
             patch.object(common, "delivery_target") as observe, \
             patch.object(common, "source_context"):

            before = dict(identity=("pid", "original"), agent="codex", composer="› prompt", busy=False)
            observe.side_effect = [before, dict(before, identity=("pid", "new"), composer="› pasted")]
            with self.assertRaises(common.DeliveryRefused) as caught:
                common.forward_prompt("%1", "question")
            self.assertEqual(caught.exception.stage, "pasted")
            self.assertEqual(command.call_count, 1)
            self.assertTrue(command.call_args.args[1].startswith("paste-buffer "))
            self.assertEqual(tmux.call_args.args[0], "delete-buffer")


if __name__ == "__main__":
    unittest.main()
