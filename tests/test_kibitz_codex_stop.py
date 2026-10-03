"""Tests for the codex-side hook: rollout mapping and the relay stash."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_tmp = tempfile.TemporaryDirectory()
os.environ["XDG_CACHE_HOME"] = _tmp.name
os.environ["TMUX_PANE"] = "%7"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

import kibitz_codex_stop as codex_hook  # noqa: E402


class CodexHookTest(unittest.TestCase):
    def run_hook(self, payload):
        os.environ["TMUX_PANE"] = "%7"
        sys.stdin = io.StringIO(json.dumps(payload))
        self.assertEqual(codex_hook.main(), 0)

    def test_session_start_records_rollout_path(self):
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "s1", "transcript_path": "/tmp/r1.jsonl"})
        self.assertEqual((codex_hook.CACHE_DIR / "pane-7.rollout").read_text(), "/tmp/r1.jsonl")
        self.assertFalse((codex_hook.CACHE_DIR / "codex-s1.msg").exists())

    def test_stop_records_rollout_and_stashes_reply(self):
        self.run_hook({"hook_event_name": "Stop", "session_id": "s2", "transcript_path": "/tmp/r2.jsonl",
                       "turn_id": "t", "last_assistant_message": "Looks right."})
        self.assertEqual((codex_hook.CACHE_DIR / "pane-7.rollout").read_text(), "/tmp/r2.jsonl")
        self.assertEqual(json.loads((codex_hook.CACHE_DIR / "codex-s2.msg").read_text())["message"], "Looks right.")


if __name__ == "__main__":
    unittest.main()
