"""Source ownership must work without launching ps, including inside a tool sandbox."""
import calendar
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
import kibitz_processes as processes
import kibitz_hook_common as common


class NativeProcessTest(unittest.TestCase):
    def test_mac_birth_dates_match_claude_and_still_reject_pid_reuse(self):
        for day in (1, 4, 9, 10, 19, 31):
            with self.subTest(day=day), tempfile.TemporaryDirectory() as directory:
                seconds = calendar.timegm((2026, 1, day, 12, 34, 56))

                def read_info(pid, flavor, arg, buffer, size):
                    info = buffer._obj
                    info.pid, info.ppid, info.seconds = pid, 1, seconds
                    info.pgid, info.tpgid, info.status = pid, pid, 2
                    info.name = b'claude'
                    return ctypes.sizeof(info)

                # Claude stores ps's space-padded calendar day, independently
                # of how the native reader chooses to format that timestamp.
                date = time.gmtime(seconds)
                saved = time.strftime('%a %b ', date) + f'{day:2d} 12:34:56 2026'
                native = dict(pid=42, kind='interactive', pidDomain='darwin',
                              procStart=saved, tmux='fixture.%1', status='idle')
                path = Path(directory) / 'sessions/42.json'
                path.parent.mkdir()
                path.write_text(json.dumps(native))
                with patch.object(processes, '_libproc', SimpleNamespace(proc_pidinfo=read_info)), \
                     patch.object(common.sys, 'platform', 'darwin'), \
                     patch.dict(os.environ, CLAUDE_CONFIG_DIR=directory):
                    current = processes.process(42)
                    self.assertEqual(common.claude_delivery_status('%1', {42: current}), {42: 'idle'})
                    seconds += 1
                    reused = processes.process(42)
                    self.assertEqual(common.claude_delivery_status('%1', {42: reused}), {})

    def test_current_identity_does_not_spawn_processes(self):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('process spawn')):
            record = processes.process(os.getpid())
            self.assertEqual(record['parent'], os.getppid())
            self.assertTrue(record['started'])
            self.assertFalse(record['stopped'])

    def test_parent_walk_stops_at_owning_server(self):
        records = {30: dict(parent=20, command='python'),
                   20: dict(parent=10, command='claude'),
                   10: dict(parent=1, command='tmux')}
        with patch.object(processes, 'process', side_effect=records.get) as read:
            self.assertEqual(processes.ancestors(30), records)
            self.assertEqual(read.call_count, 3)

    def test_missing_or_dead_process_is_not_invented(self):
        self.assertIsNone(processes.process(-1))
        self.assertIsNone(processes.process(999999999))

    def test_linux_foreground_and_birth_are_read_from_proc(self):
        fields = ['S', '11', '12', '12', '34', '12'] + ['0'] * 13 + ['87654'] + ['0'] * 10
        raw = '12 (codex) ' + ' '.join(fields)
        with patch.object(processes, '_libproc', None), patch.object(processes.sys, 'platform', 'linux'), \
             patch.object(Path, 'read_text', return_value=raw):
            record = processes.process(12)
        self.assertEqual(record['parent'], 11)
        self.assertTrue(record['foreground'])
        self.assertEqual(record['started'], '87654')
        self.assertEqual(record['tty'], 34)


if __name__ == '__main__':
    unittest.main()
