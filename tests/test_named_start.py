"""Named Codex creation uses metadata RPCs, handles failure, and never runs a turn."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("kibitz_codex_start", ROOT / "scripts/kibitz_codex_start.py")
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)
CREATED = "01900000-1111-7222-8333-000000000004"


class NamedStartTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.bins = self.home / "bin"
        self.bins.mkdir()
        fake = self.bins / "codex"
        fake.write_text("#!/usr/bin/env python3\nimport runpy\nrunpy.run_path(" +
                        repr(str(ROOT / "tests/fake_named_codex.py")) + ", run_name='__main__')\n")
        fake.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / ".codex"),
                        PATH=str(self.bins) + os.pathsep + os.environ["PATH"],
                        KIBITZ_TEST_CODEX_SCENARIO="")

    def requests(self):
        return [json.loads(line) for line in (self.home / "codex-start-calls.jsonl").read_text().splitlines()]

    def create(self, name="named reviewer", timeout=5):
        with patch.dict(os.environ, self.env, clear=True):
            return startup.create_named_thread(name, timeout=timeout)

    def test_creates_names_and_confirms_without_starting_a_turn(self):
        self.assertEqual(self.create(), CREATED)
        self.assertEqual([row["method"] for row in self.requests()],
                         ["initialize", "initialized", "thread/start", "thread/name/set", "thread/read"])

    def test_partial_failure_archives_only_the_new_empty_thread(self):
        for scenario in ("thread/name/set", "wrong-name"):
            with self.subTest(scenario=scenario):
                self.env["KIBITZ_TEST_CODEX_SCENARIO"] = scenario
                with self.assertRaises(RuntimeError):
                    self.create()
                last = self.requests()[-1]
                self.assertEqual(last["method"], "thread/archive")
                self.assertEqual(last["params"], {"threadId": CREATED})

    def test_error_before_thread_creation_never_archives_another_thread(self):
        self.env["KIBITZ_TEST_CODEX_SCENARIO"] = "thread/start"
        with self.assertRaises(RuntimeError):
            self.create()
        self.assertNotIn("thread/archive", [row["method"] for row in self.requests()])

    def test_unverified_id_never_reaches_the_launcher(self):
        self.env["KIBITZ_TEST_CODEX_SCENARIO"] = "invalid-id"
        with self.assertRaises(ValueError):
            self.create()
        self.assertNotIn("thread/name/set", [row["method"] for row in self.requests()])

    def test_timeout_stops_startup(self):
        self.env["KIBITZ_TEST_CODEX_SCENARIO"] = "timeout"
        with self.assertRaises(TimeoutError):
            self.create(timeout=0.1)

    def test_blank_name_does_not_start_app_server(self):
        with self.assertRaises(ValueError):
            self.create("  ")
        self.assertFalse((self.home / "codex-start-calls.jsonl").exists())

    def test_cli_returns_nonzero_and_no_session_id_on_failure(self):
        self.env["KIBITZ_TEST_CODEX_SCENARIO"] = "thread/start"
        result = subprocess.run(["python3", str(ROOT / "scripts/kibitz_codex_start.py"), "name"],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Could not create named Codex reviewer", result.stderr)


if __name__ == "__main__":
    unittest.main()
