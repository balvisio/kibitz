"""Check fresh and resumed reviewer launches without starting real agents."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parent.parent / "scripts/kibitz_reviewer_launch"


class ReviewerLaunchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        bins = self.home / "bin"
        bins.mkdir()
        for kind in ("codex", "claude"):
            path = bins / kind
            path.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(bins) + ":" + os.environ["PATH"])
        self.role = self.home / ".codex/systemprompts/kibitz-reviewer.md"
        self.role.parent.mkdir(parents=True)
        self.role.write_text('Reviewer role with "quotes"\nand two lines.')
        self.agent = self.home / ".claude/agents/kibitz-reviewer.md"
        self.agent.parent.mkdir(parents=True)
        self.agent.write_text("Reviewer role")

    def launch(self, *args):
        result = subprocess.run([str(LAUNCHER), *args], env=self.env, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_codex_fresh_and_resume_keep_role_and_settings(self):
        flags = ["--no-daemon", "--config", "sandbox_workspace_write.network_access=true",
                 "--config", "developer_instructions=" + self.role.read_text()]
        self.assertEqual(self.launch("codex"), flags)
        self.assertEqual(self.launch("resume", "codex", "thread-one"), flags + ["resume", "thread-one"])

    def test_claude_fresh_and_resume_keep_role(self):
        self.assertEqual(self.launch("claude"), ["--agent", "kibitz-reviewer"])
        self.assertEqual(self.launch("resume", "claude", "session-one"),
                         ["--agent", "kibitz-reviewer", "--resume", "session-one"])

    def test_resume_without_role_files_still_resumes(self):
        self.role.unlink()
        self.agent.unlink()
        self.assertEqual(self.launch("resume", "codex", "thread-one"),
                         ["--no-daemon", "--config", "sandbox_workspace_write.network_access=true", "resume", "thread-one"])
        self.assertEqual(self.launch("resume", "claude", "session-one"), ["--resume", "session-one"])

    def test_invalid_resume_never_starts_a_fresh_conversation(self):
        for args in (("resume", "codex"), ("resume", "claude", ""),
                     ("codex", "resume", "thread-one"), ("resume", "unknown", "thread-one")):
            with self.subTest(args=args):
                result = subprocess.run([str(LAUNCHER), *args], env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(result.stdout)


if __name__ == "__main__":
    unittest.main()
