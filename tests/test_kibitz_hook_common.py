"""Tests for the shared sender: busy detection from codex's rollout log and
the Tab-versus-Enter choice."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import kibitz_hook_common as common  # noqa: E402


def event(kind):
    return json.dumps({"timestamp": "t", "type": "event_msg", "payload": {"type": kind}})


class SenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved_cache, self.saved_run = common.CACHE_DIR, common.subprocess.run
        common.CACHE_DIR = root / "cache"
        common.CACHE_DIR.mkdir()
        self.rollout = root / "rollout.jsonl"
        (common.CACHE_DIR / "pane-7.rollout").write_text(str(self.rollout))
        self.calls = []
        common.subprocess.run = lambda args, **kw: self.calls.append(args)

    def tearDown(self):
        common.CACHE_DIR, common.subprocess.run = self.saved_cache, self.saved_run
        self.tmp.cleanup()

    def write_rollout(self, *kinds):
        self.rollout.write_text("\n".join(['{"type": "session_meta"}'] + [event(k) for k in kinds]) + "\n")

    def test_busy_follows_the_last_task_event(self):
        self.write_rollout("task_started", "task_complete", "task_started")
        self.assertTrue(common.reviewer_busy("%7"))
        self.write_rollout("task_started", "task_complete")
        self.assertFalse(common.reviewer_busy("%7"))
        self.write_rollout("task_started", "turn_aborted")
        self.assertFalse(common.reviewer_busy("%7"))

    def test_long_output_after_the_event_does_not_hide_it(self):
        big = json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * 200_000}})
        self.rollout.write_text("\n".join([event("task_complete"), event("task_started"), big, big]) + "\n")
        self.assertTrue(common.reviewer_busy("%7"))
        self.rollout.write_text("\n".join([event("task_started"), event("task_complete"), big, big]) + "\n")
        self.assertFalse(common.reviewer_busy("%7"))
        self.rollout.write_text("\n".join([event("task_started"), big[:70_000] + '"}}', big]) + "\n")
        self.assertTrue(common.reviewer_busy("%7"))

    def test_unknown_pane_or_missing_log_is_not_busy(self):
        self.assertFalse(common.reviewer_busy("%8"))
        self.assertFalse(common.reviewer_busy("%7"))

    def test_forward_submits_with_tab_while_busy_unless_forced(self):
        self.write_rollout("task_started")
        common.forward("%7", "hello")
        self.assertEqual(self.calls[-1], ["tmux-bridge", "keys", "%7", "Tab"])
        common.forward("%7", "hello", force=True)
        self.assertEqual(self.calls[-1], ["tmux-bridge", "keys", "%7", "Enter"])
        self.write_rollout("task_started", "task_complete")
        common.forward("%7", "hello")
        self.assertEqual(self.calls[-1], ["tmux-bridge", "keys", "%7", "Enter"])


if __name__ == "__main__":
    unittest.main()
