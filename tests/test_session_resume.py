"""Named-host startup, using isolated metadata and fake tmux/agent processes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("kibitz_session", ROOT / "scripts/kibitz_session.py")
lookup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lookup
spec.loader.exec_module(lookup)

HOST = "01900000-1111-7222-8333-000000000001"
REVIEWER = "01900000-1111-7222-8333-000000000002"
OTHER = "01900000-1111-7222-8333-000000000003"
CREATED = "01900000-1111-7222-8333-000000000004"


class SessionResumeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.work = self.home / "project"
        self.work.mkdir()
        self.codex = self.home / ".codex"
        self.claude = self.home / ".claude"
        self.bins = self.home / "bin"
        self.bins.mkdir()
        for binary in ("bash", "python3", "awk", "basename", "dirname", "cat"):
            (self.bins / binary).symlink_to(shutil.which(binary))
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bins),
                        PWD=str(self.work), TMUX="test-socket,1,0", TMUX_PANE="%1",
                        CLAUDECODE="1", CODEX_THREAD_ID="", CODEX_HOME=str(self.codex),
                        XDG_CACHE_HOME=str(self.home / "cache"),
                        XDG_STATE_HOME=str(self.home / "state"))
        self.env.pop("CLAUDE_CONFIG_DIR", None)
        self.calls_path = self.home / "tmux-calls.jsonl"
        self.argv_path = self.home / "agent-argv.json"
        self.context_path = self.home / "agent-context.json"
        (self.home / "tmux-server-env.json").write_text(json.dumps(
            {"CLAUDE_CONFIG_DIR": "stale-server-profile"}))
        self.write_executable("tmux", '''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
home = Path(os.environ["HOME"])
args = sys.argv[1:]
with (home / "tmux-calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\\n")
existing = home / "existing-reviewer"
if args[0] == "display-message":
    assert args[-1] == "#{pane_pid}", args
    print(100)
elif args[0] == "list-panes":
    if existing.exists():
        print("%3 codex")
elif args[0] == "kill-pane":
    existing.unlink()
elif args[0] == "split-window":
    directory = args[args.index("-c") + 1]
    assert "CODEX_HOME=" + os.environ["CODEX_HOME"] in args, args
    child_env = dict(os.environ, PWD=directory)
    child_env.pop("CLAUDE_CONFIG_DIR", None)
    child_env.update(json.loads((home / "tmux-server-env.json").read_text()))
    for index, arg in enumerate(args):
        if arg == "-e":
            key, value = args[index + 1].split("=", 1)
            child_env[key] = value
    subprocess.run(["/bin/sh", "-c", args[-1]], check=True, env=child_env, cwd=directory)
    print("%9")
elif args[0] != "set-option":
    sys.exit("unexpected tmux command: " + repr(args))
''')
        for agent in ("codex", "claude"):
            self.write_executable(agent, '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
''' + f'sys.path.insert(0, {str(ROOT / "tests")!r})\n' + '''
if sys.argv[1:2] == ["app-server"]:
    from fake_named_codex import app_server
    app_server()
    sys.exit(0)
Path(os.environ["HOME"], "agent-argv.json").write_text(json.dumps(sys.argv))
Path(os.environ["HOME"], "agent-context.json").write_text(json.dumps({
    "cwd": os.getcwd(), "claude_config_present": "CLAUDE_CONFIG_DIR" in os.environ,
    "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
    "reviewer_name_present": "KIBITZ_REVIEWER_NAME" in os.environ}))
''')
        self.write_executable("tmux-bridge", "#!/bin/sh\nexit 0\n")
        self.launcher = self.bins / "kibitz_reviewer_launch"
        self.launcher.symlink_to(ROOT / "scripts/kibitz_reviewer_launch")
        (self.bins / "kibitz_codex_start.py").symlink_to(ROOT / "scripts/kibitz_codex_start.py")
        self.role = self.codex / "systemprompts/kibitz-reviewer.md"
        self.role.parent.mkdir(parents=True)
        self.role.write_text('Reviewer role with "quotes"\nand two lines.')
        agent_file = self.claude / "agents/kibitz-reviewer.md"
        agent_file.parent.mkdir(parents=True)
        agent_file.write_text("Reviewer role")
        self.started = "Wed Nov  4 18:28:53 2026"
        self.pid = os.getpid()
        self.processes = {100: (1, self.started), self.pid: (100, " ".join(self.started.split()))}
        self.write_executable("ps", "#!/bin/sh\nprintf '%s\\n' "
                              + "'100 1 Wed Nov 4 18:28:00 2026 bash' "
                              + f"'{self.pid} 100 {self.started} claude'\n")
        self.host_record("my review")

    def write_executable(self, name, body):
        path = self.bins / name
        path.write_text(body)
        path.chmod(0o755)

    def host_record(self, name, **extra):
        row = dict(sessionId=HOST, pid=self.pid, procStart=lookup.process_start(self.pid, self.processes),
                   pidDomain=lookup.pid_domain(), kind="interactive", tmux="main:@1.%1",
                   name=name, nameSource="user", cwd=str(self.work))
        row.update(extra)
        path = self.claude / "sessions" / f"{self.pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row))
        return path

    def codex_session(self, name="my review", identity=REVIEWER, cwd=None, **extra):
        cwd = str(cwd or self.work)
        path = self.codex / "sessions/2026/11/16" / f"rollout-2026-11-16-{identity}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(type="session_meta", payload=dict(id=identity, cwd=cwd))) + "\n")
        with sqlite3.connect(self.codex / "state_5.sqlite") as db:
            db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, "
                       "preview TEXT, cwd TEXT, rollout_path TEXT, archived INTEGER)")
            db.execute("INSERT OR REPLACE INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (identity, name, extra.get("title", "default title"), extra.get("preview", "preview"),
                        cwd, str(path), extra.get("archived", 0)))
        return path

    def claude_session(self, name="my review", identity=REVIEWER, cwd=None, events=None):
        path = self.claude / "projects" / ("project-" + identity) / f"{identity}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = [dict(type="user", sessionId=identity, cwd=str(cwd or self.work), message={}),
                   dict(type="custom-title", sessionId=identity, customTitle=name)]
        entries.extend(events or [])
        path.write_text("".join(json.dumps(row) + "\n" for row in entries))
        return path

    def use_codex_host(self, name="my review", cwd=None):
        self.env.update(CLAUDECODE="", CODEX_THREAD_ID=HOST)
        self.codex_session(name, identity=HOST, cwd=cwd)

    def set_host_for(self, reviewer, name="my review", cwd=None):
        if reviewer == "codex":
            self.env.update(CLAUDECODE="1", CODEX_THREAD_ID="")
            self.host_record(name, cwd=str(cwd or self.work))
        else:
            self.use_codex_host(name, cwd=cwd)

    def save_reviewer(self, reviewer, **kwargs):
        return (self.codex_session if reviewer == "codex" else self.claude_session)(**kwargs)

    def run_cli(self, *args, cwd=None):
        self.calls_path.write_text("")
        (self.home / "codex-start-calls.jsonl").write_text("")
        if self.argv_path.exists():
            self.argv_path.unlink()
        if self.context_path.exists():
            self.context_path.unlink()
        cwd = cwd or self.work
        return subprocess.run([str(ROOT / "kibitz"), *args], env=dict(self.env, PWD=str(cwd)), cwd=cwd,
                              text=True, capture_output=True, timeout=15)

    def assert_launch(self, result, agent, resume=None, role=True, directory=None, fresh_name=None):
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        argv = json.loads(self.argv_path.read_text())
        self.assertEqual(Path(argv[0]).name, agent)
        flags = []
        if agent == "codex":
            flags = ["--no-daemon"]
            if role:
                flags += ["--config", "sandbox_workspace_write.network_access=true",
                          "--config", "developer_instructions=" + self.role.read_text().rstrip("\n")]
        elif role:
            flags = ["--agent", "kibitz-reviewer"]
        if resume:
            flags += ["resume" if agent == "codex" else "--resume", resume]
            self.assertIn("resuming", result.stdout)
        else:
            self.assertIn("started", result.stdout)
            if fresh_name:
                flags += ["resume", CREATED] if agent == "codex" else ["--name", fresh_name]
                if agent == "codex":
                    requests = [json.loads(line) for line in (self.home / "codex-start-calls.jsonl").read_text().splitlines()]
                    rename = next(row for row in requests if row["method"] == "thread/name/set")
                    self.assertEqual(rename["params"], {"threadId": CREATED, "name": fresh_name})
                    self.assertFalse(any(row["method"] == "turn/start" for row in requests))
        self.assertEqual(argv[1:], flags)
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
        label = "codex" if agent == "codex" else "claude-reviewer"
        self.assertIn(["set-option", "-p", "-t", "%9", "@name", label], calls)
        self.assertIn(["set-option", "-p", "-t", "%9", "@kibitz_host_pane", "%1"], calls)
        context = json.loads(self.context_path.read_text())
        self.assertEqual(Path(context["cwd"]), (directory or self.work).resolve())
        self.assertEqual(context["claude_config_present"], "CLAUDE_CONFIG_DIR" in self.env)
        self.assertEqual(context["claude_config_dir"], self.env.get("CLAUDE_CONFIG_DIR"))
        if role:
            self.assertFalse(context["reviewer_name_present"])

    def test_named_claude_resumes_named_codex_with_role_and_pairing(self):
        self.codex_session()
        self.assert_launch(self.run_cli("start"), "codex", REVIEWER)

    def test_named_codex_resumes_named_claude_with_role_and_pairing(self):
        self.use_codex_host()
        self.claude_session()
        self.assert_launch(self.run_cli("start"), "claude", REVIEWER)

    def test_missing_name_match_starts_fresh_for_both_agents(self):
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")
        self.use_codex_host()
        self.assert_launch(self.run_cli("start", "claude"), "claude", fresh_name="my review")

    def test_derived_or_unnamed_host_never_resumes(self):
        self.codex_session()
        for source in ("derived", "unknown", None):
            with self.subTest(source=source):
                self.host_record("my review", nameSource=source)
                self.assert_launch(self.run_cli("start", "codex"), "codex")
        self.host_record("")
        self.assert_launch(self.run_cli("start", "codex"), "codex")
        self.use_codex_host(None)
        self.claude_session("default title")
        self.assert_launch(self.run_cli("start", "claude"), "claude")

    def test_other_directories_and_partial_names_are_ignored(self):
        self.codex_session(cwd=self.home / "other")
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")
        self.codex_session("my review extended")
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")
        self.use_codex_host()
        self.claude_session(cwd=self.home / "other")
        self.assert_launch(self.run_cli("start", "claude"), "claude", fresh_name="my review")

    def test_duplicate_name_starts_fresh_with_a_message(self):
        self.codex_session()
        self.codex_session(identity=OTHER)
        result = self.run_cli("start", "codex")
        self.assert_launch(result, "codex")
        self.assertIn("Multiple reviewer sessions match", result.stderr)

    def test_archived_and_deleted_codex_sessions_are_ignored(self):
        path = self.codex_session(archived=1)
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")
        self.codex_session()
        path.unlink()
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")

    def test_names_are_matched_as_data_and_only_ids_reach_the_shell(self):
        name = "review's \"name\" $(>injected) `>injected`; *\nnext"
        self.host_record(name)
        self.codex_session(name)
        self.assert_launch(self.run_cli("start", "codex"), "codex", REVIEWER)
        self.assertFalse((self.work / "injected").exists())

    def test_new_names_with_metacharacters_are_passed_as_data_for_both_agents(self):
        name = "review's \"name\" $(>injected) `>injected`; *\nnext"
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent, name=name)
                self.assert_launch(self.run_cli("start", agent), agent, fresh_name=name)
                self.assertFalse((self.work / "injected").exists())

    def test_new_named_codex_is_found_on_next_start_without_manual_rename(self):
        self.env["KIBITZ_TEST_PERSIST_CREATED"] = "1"
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")
        self.assert_launch(self.run_cli("restart", "codex"), "codex", CREATED)
        self.assertEqual((self.home / "codex-start-calls.jsonl").read_text(), "")

    def test_failed_named_start_does_not_launch_an_unnamed_codex(self):
        self.env["KIBITZ_TEST_CODEX_SCENARIO"] = "thread/name/set"
        result = self.run_cli("start", "codex")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not create named Codex reviewer", result.stderr)
        self.assertFalse(self.argv_path.exists())

    def test_stale_pid_different_namespace_or_other_pane_is_not_the_host(self):
        self.codex_session()
        for extra in (dict(procStart="old"), dict(pidDomain="other"), dict(tmux="main:@1.%2"),
                      dict(pid=999999), dict(kind="subagent")):
            with self.subTest(extra=extra):
                self.host_record("my review", **extra)
                self.assert_launch(self.run_cli("start", "codex"), "codex")

    def test_claude_host_takes_precedence_over_inherited_codex_identity(self):
        self.env["CODEX_THREAD_ID"] = HOST
        self.codex_session("different name", identity=HOST)
        self.codex_session()
        self.assert_launch(self.run_cli("start", "codex"), "codex", REVIEWER)

    def test_custom_config_directories_are_used(self):
        self.claude = self.home / "custom claude"
        self.codex = self.home / "custom codex"
        self.env.update(CLAUDE_CONFIG_DIR=str(self.claude), CODEX_HOME=str(self.codex))
        self.host_record("my review")
        self.codex_session()
        self.assert_launch(self.run_cli("start", "codex"), "codex", REVIEWER)

    def test_unset_claude_config_removes_stale_server_value_for_both_reviewers(self):
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.env)
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent)
                self.assert_launch(self.run_cli("start", agent), agent, fresh_name="my review")
                self.save_reviewer(agent)
                self.assert_launch(self.run_cli("start", agent), agent, REVIEWER)
                calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()]
                split = next(call for call in calls if call[0] == "split-window")
                self.assertFalse(any(arg.startswith("CLAUDE_CONFIG_DIR=") for arg in split))

    def test_explicit_empty_claude_config_is_preserved_for_both_reviewers(self):
        self.env["CLAUDE_CONFIG_DIR"] = ""
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent)
                self.assert_launch(self.run_cli("start", agent), agent, fresh_name="my review")
                self.save_reviewer(agent)
                self.assert_launch(self.run_cli("start", agent), agent, REVIEWER)

    def test_custom_claude_config_is_preserved_for_both_reviewers(self):
        self.claude = self.home / "claude's custom profile $literal\n"
        self.env["CLAUDE_CONFIG_DIR"] = str(self.claude)
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent)
                self.assert_launch(self.run_cli("start", agent), agent, fresh_name="my review")
                self.save_reviewer(agent)
                self.assert_launch(self.run_cli("start", agent), agent, REVIEWER)

    def test_temporary_shell_directory_does_not_change_host_match(self):
        shell_directory = self.home / "temporary-shell-directory"
        shell_directory.mkdir()
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent)
                self.save_reviewer(agent)
                self.save_reviewer(agent, identity=OTHER, cwd=shell_directory)
                self.assert_launch(self.run_cli("start", agent, cwd=shell_directory), agent, REVIEWER)

    def test_match_in_shell_directory_does_not_override_host_directory(self):
        shell_directory = self.home / "temporary-shell-directory"
        shell_directory.mkdir()
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent)
                self.save_reviewer(agent, cwd=shell_directory)
                self.assert_launch(self.run_cli("start", agent, cwd=shell_directory), agent, fresh_name="my review")

    def test_fresh_and_unnamed_reviewers_launch_in_known_host_directory(self):
        shell_directory = self.home / "temporary-shell-directory"
        shell_directory.mkdir()
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent)
                self.assert_launch(self.run_cli("start", agent, cwd=shell_directory), agent, fresh_name="my review")
                self.set_host_for(agent, name="")
                self.save_reviewer(agent, name="default title")
                self.assert_launch(self.run_cli("start", agent, cwd=shell_directory), agent)

    def test_missing_or_unknown_host_directory_starts_fresh_in_shell_directory(self):
        shell_directory = self.home / "temporary-shell-directory"
        shell_directory.mkdir()
        for agent in ("codex", "claude"):
            for host_directory in (self.home / "deleted-project", Path("relative-project")):
                with self.subTest(agent=agent, directory=host_directory):
                    self.set_host_for(agent, cwd=host_directory)
                    self.save_reviewer(agent, cwd=shell_directory)
                    self.assert_launch(self.run_cli("start", agent, cwd=shell_directory), agent,
                                       directory=shell_directory)

    def test_host_directory_survives_lookup_errors(self):
        shell_directory = self.home / "temporary-shell-directory"
        shell_directory.mkdir()
        (self.codex / "state_5.sqlite").write_bytes(b"not a database")
        self.assert_launch(self.run_cli("start", "codex", cwd=shell_directory), "codex")

    def test_directory_whitespace_and_metacharacters_survive_without_evaluation(self):
        directory = self.home / "project's \"name\" $(>injected) `>injected`; *\n"
        directory.mkdir()
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                self.set_host_for(agent, cwd=directory)
                self.save_reviewer(agent, cwd=directory)
                self.assert_launch(self.run_cli("start", agent), agent, REVIEWER, directory=directory)
                self.assertFalse((directory / "injected").exists())
                self.assertFalse((self.work / "injected").exists())

    def test_restart_reuses_the_match_and_start_remains_a_noop(self):
        self.codex_session()
        existing = self.home / "existing-reviewer"
        existing.touch()
        result = self.run_cli("start", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already running", result.stdout)
        self.assertFalse(self.argv_path.exists())
        self.assert_launch(self.run_cli("restart", "codex"), "codex", REVIEWER)

    def test_fallback_without_launcher_still_resumes(self):
        self.launcher.unlink()
        self.codex_session()
        self.assert_launch(self.run_cli("start", "codex"), "codex", REVIEWER, role=False)

    def test_corrupt_lookup_metadata_starts_fresh(self):
        (self.codex / "state_5.sqlite").write_bytes(b"not a database")
        result = self.run_cli("start", "codex")
        self.assert_launch(result, "codex")
        self.assertIn("Cannot check saved reviewer sessions", result.stderr)

    def test_latest_custom_title_wins_and_ai_titles_are_ignored(self):
        path = self.claude_session(events=[dict(type="custom-title", sessionId=REVIEWER, customTitle="new")])
        deadline = time.monotonic() + 5
        self.assertEqual(lookup.claude_saved_session(path, deadline).name, "new")
        path = self.claude_session(name="", events=[dict(type="ai-title", sessionId=REVIEWER, aiTitle="my review"),
                                                    dict(type="agent-name", sessionId=REVIEWER, agentName="my review")])
        self.assertIsNone(lookup.claude_saved_session(path, deadline))

    def test_custom_title_before_long_history_is_still_found(self):
        path = self.claude_session()
        with path.open("a") as stream:
            stream.write(json.dumps(dict(type="assistant", message="x" * (2 * 1024 * 1024))) + "\n")
        session = lookup.claude_saved_session(path, time.monotonic() + 5)
        self.assertEqual(session.name, "my review")
        self.assertEqual(session.cwd, str(self.work))

    def test_symlink_paths_match_but_the_host_itself_is_excluded(self):
        alias = self.home / "alias"
        alias.symlink_to(self.work, target_is_directory=True)
        path = self.codex_session()
        host = lookup.Session("codex", HOST, "my review", str(self.work))
        candidate = lookup.Session("codex", REVIEWER, "my review", str(alias), path)
        self.assertEqual(lookup.matching_session(host, "codex", [candidate]), candidate)
        own = lookup.Session("codex", HOST, "my review", str(self.work), path)
        self.assertIsNone(lookup.matching_session(host, "codex", [own]))

    def test_legacy_index_uses_latest_name_and_requires_a_saved_rollout(self):
        path = self.codex_session()
        (self.codex / "state_5.sqlite").unlink()
        index = self.codex / "session_index.jsonl"
        index.write_text("not json\n" + json.dumps(dict(id=REVIEWER, thread_name="old")) + "\n"
                         + json.dumps(dict(id=REVIEWER, thread_name="my review")) + "\n"
                         + json.dumps(dict(id=OTHER, thread_name="missing rollout")) + "\n")
        self.assertEqual(lookup.codex_sessions(self.codex),
                         [lookup.Session("codex", REVIEWER, "my review", str(self.work), path)])

    def test_database_unset_name_overrides_stale_index_name(self):
        self.codex_session(None)
        (self.codex / "session_index.jsonl").write_text(json.dumps(dict(id=REVIEWER, thread_name="my review")) + "\n")
        self.assert_launch(self.run_cli("start", "codex"), "codex", fresh_name="my review")

    def test_deadline_expiration_abandons_lookup(self):
        path = self.claude_session()
        with self.assertRaises(TimeoutError):
            lookup.claude_saved_session(path, time.monotonic() - 1)

    def test_installer_copies_helper_and_uninstaller_removes_it(self):
        env = dict(self.env, PATH=str(self.bins) + os.pathsep + os.defpath)
        result = subprocess.run([str(ROOT / "kibitz"), "install"], env=env, cwd=self.work,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        installed = self.home / ".local/bin"
        self.assertEqual((installed / "kibitz_session.py").read_bytes(),
                         (ROOT / "scripts/kibitz_session.py").read_bytes())
        self.assertEqual((installed / "kibitz_codex_start.py").read_bytes(),
                         (ROOT / "scripts/kibitz_codex_start.py").read_bytes())
        self.codex_session()
        result = subprocess.run([str(installed / "kibitz"), "start", "codex"], env=env,
                                cwd=self.work, text=True, capture_output=True, timeout=15)
        self.assert_launch(result, "codex", REVIEWER)
        result = subprocess.run([str(installed / "kibitz"), "uninstall"], env=env, cwd=self.work,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((installed / "kibitz_session.py").exists())
        self.assertFalse((installed / "kibitz_codex_start.py").exists())


if __name__ == "__main__":
    unittest.main()
