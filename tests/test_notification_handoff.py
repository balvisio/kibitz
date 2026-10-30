"""The hook decisions must preserve pending reviews until actual completion."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
import kibitz_notifications as notifications
import kibitz_hook_stop as stop
import kibitz_hook_user_prompt_submit as prompt


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(notifications, 'CACHE_DIR', self.root/'cache'); p.start(); self.addCleanup(p.stop)
        self.events = []
        self.sent = []
        for module in (stop, prompt):
            for name, value in (
                ('notify', lambda action, payload, *args: self.events.append((action, payload['prompt_id']))),
                ('resolve_reviewer', lambda: ('%9', 'codex')),
                ('current_pane_label', lambda: ''),
                ('forward', lambda pane, message, **kw: self.sent.append(message)),
            ):
                p = patch.object(module, name, value); p.start(); self.addCleanup(p.stop)
        p=patch.object(stop, 'LAST_FORWARD_PATH', self.root/'last');p.start();self.addCleanup(p.stop)
        self.payload = dict(session_id='session', prompt_id='one')

    def submit(self, text):
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(dict(self.payload, prompt=text)))):
            prompt.main()

    def finish(self, text='question', background=False, owner='one'):
        entries=[dict(type='user', promptId=owner, origin={'kind':'human'},
                      message={'role':'user','content':text})]
        if background:
            entries.append(dict(type='user', message={'role':'user', 'content':[dict(type='tool_result',content='working')]},
                                toolUseResult={'backgroundTaskId':'task'}))
        transcript=self.root/'transcript'
        transcript.write_text(''.join(json.dumps(entry)+'\n' for entry in entries))
        data=dict(self.payload, transcript_path=str(transcript), last_assistant_message='answer',
                  background_tasks=[{'id':'task'}] if background else [])
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(data))):
            stop.main()

    def test_normal_turn_reserves_before_forwarding(self):
        self.submit('question')
        self.assertEqual(self.events, [('expect','one')])
        self.finish()
        self.assertEqual(self.events, [('expect','one'),('queue','one')])
        self.assertEqual(len(self.sent),1)

    def test_mute_is_an_explicit_skip(self):
        self.submit('question /mute')
        self.finish('question /mute')
        self.assertEqual(self.events, [('expect','one'),('skip','one'),('skip','one')])
        self.assertFalse(self.sent)

    def test_tee_stop_does_not_clear_queued_review(self):
        self.submit('question /tee')
        self.finish('question /tee')
        self.assertEqual(self.events, [('expect','one'),('queue','one')])
        self.assertEqual(len(self.sent),1)

    def test_interim_and_duplicate_stops_do_not_release_expectation(self):
        self.submit('question')
        self.finish(background=True)
        self.assertEqual(self.events, [('expect','one')])
        self.finish()
        self.finish()
        self.assertEqual(self.events, [('expect','one'),('queue','one')])

    def test_background_reply_uses_owning_prompt_identity(self):
        self.payload['prompt_id']='later'
        self.finish(owner='original')
        self.assertEqual(self.events, [('queue','original')])

    def test_background_wakeups_do_not_reserve_or_cache_a_new_prompt(self):
        self.submit('question')
        self.finish(background=True)
        for task in ('task', 'nested'):
            self.payload['prompt_id'] = task + '-finished'
            self.submit('  <task-notification><task-id>' + task + '</task-id></task-notification>')
            self.assertIsNone(notifications.remembered_prompt(self.payload))
        self.assertEqual(self.events, [('expect', 'one')])
        self.finish(owner='one')
        self.assertEqual(self.events, [('expect', 'one'), ('queue', 'one')])
        self.assertEqual(len(self.sent), 1)

    def test_unknown_task_notification_does_not_create_an_expectation(self):
        self.submit('<task-notification><task-id>unknown</task-id></task-notification>')
        self.assertFalse(self.events)
        self.assertIsNone(notifications.remembered_prompt(self.payload))

    def test_task_notification_mentioned_in_user_prompt_is_forwarded(self):
        self.submit('Explain <task-notification>')
        self.finish('Explain <task-notification>')
        self.assertEqual(self.events, [('expect', 'one'), ('queue', 'one')])

    def test_identical_exchanges_in_different_prompts_are_both_forwarded(self):
        self.submit('question')
        self.finish()
        self.payload['prompt_id']='two'
        self.submit('question')
        self.finish(owner='two')
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.events, [('expect','one'),('queue','one'),('expect','two'),('queue','two')])

    def test_fast_reply_uses_prompt_capture_before_transcript_flush(self):
        self.submit('fresh prompt')
        transcript=self.root/'transcript'
        transcript.write_text('')
        data=dict(self.payload, transcript_path=str(transcript), last_assistant_message='answer')
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(data))):
            stop.main()
        self.assertEqual(self.events, [('expect','one'),('queue','one')])
        self.assertIn('USER:\nfresh prompt', self.sent[0])
        self.assertIsNone(notifications.remembered_prompt(self.payload))

    def test_delivery_failure_is_reported_without_clearing_pending_review(self):
        self.submit('question')
        with patch.object(stop,'forward',side_effect=RuntimeError('fixture failure')), patch.object(stop,'log'):
            self.finish()
        self.assertEqual(self.events, [('expect','one'),('queue','one'),('fail','one')])


if __name__ == '__main__':
    unittest.main()
