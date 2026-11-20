"""Tests for the shared sender: busy detection from codex's rollout log and
the Tab-versus-Enter choice."""
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
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
        self.target = dict(identity=["10", "/dev/pts/1", 11, "new", "codex"])
        self.record = dict(path=str(self.rollout), server=common.os.environ.get("TMUX", "").rsplit(",", 1)[0],
                           identity=self.target["identity"])
        self.mapping = common.CACHE_DIR / "pane-7.rollout"
        self.mapping.write_text(json.dumps(self.record))
        observer = patch.object(common, "delivery_target", return_value=self.target)
        observer.start()
        self.addCleanup(observer.stop)
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

    def test_stale_process_server_and_legacy_mappings_are_not_trusted(self):
        self.write_rollout("task_started")
        for data in (dict(self.record, server="other-server"),
                     dict(self.record, identity=["10", "/dev/pts/1", 11, "old", "codex"]),
                     str(self.rollout)):
            self.mapping.write_text(json.dumps(data))
            self.assertFalse(common.reviewer_busy("%7", self.target))

    def test_all_automatic_sends_use_the_guard(self):
        with patch.object(common, "forward_prompt") as guarded:
            common.forward("%7", "hello", force=True)
            guarded.assert_called_once_with("%7", "hello", expected_agent=None,
                                            force=True, before_paste=None)


if __name__ == "__main__":
    unittest.main()
