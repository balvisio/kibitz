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


def task_attachment(task, status='completed'):
    return dict(type='attachment', attachment=dict(
        type='queued_command', commandMode='task-notification',
        origin={'kind': 'task-notification', 'producer': 'session-task'},
        prompt=f'<task-notification><task-id>{task}</task-id>'
               f'<status>{status}</status><summary>fixture</summary></task-notification>'))


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
                ('notify', lambda action, payload, *args, **kw: (self.events.extend((action, p) for p in kw.get('prompt_ids', [payload['prompt_id']])) or True)),
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

    def abandoned_fixture(self, tasks=(), sibling=False, failed=False, unmatched=False):
        self.submit('old question')
        entries = [dict(type='user', promptId='one', uuid='old', parentUuid='anchor',
                        message={'content': 'old question'})]
        for task in tasks:
            entries.append(dict(type='user', message={'content': []},
                                toolUseResult={'backgroundTaskId': task}))
        self.payload['prompt_id'] = 'two'
        self.submit('new question')
        entries.append(dict(type='user', promptId='two', uuid='new',
                            parentUuid='anchor' if sibling else 'old', message={'content': 'new question'}))
        for task in tasks:
            entries.extend([
                dict(type='assistant', message={'content': [dict(type='tool_use', id='stop-' + task,
                    name='TaskStop', input={'task_id': task})]}),
                dict(type='user', message={'content': [dict(type='tool_result',
                    tool_use_id='missing' if unmatched else 'stop-' + task, is_error=failed)]},
                    toolUseResult={'task_id': task}),
            ])
        return entries

    def stop_entries(self, entries, pending=()):
        transcript = self.root / 'transcript'
        transcript.write_text(''.join(json.dumps(e) + '\n' for e in entries))
        data = dict(self.payload, transcript_path=str(transcript), last_assistant_message='answer',
                    background_tasks=[{'id': t} for t in pending])
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(data))):
            stop.main()

    def test_replaced_prompt_releases_only_its_expectation(self):
        entries = self.abandoned_fixture(sibling=True)
        self.stop_entries(entries)
        self.assertEqual(self.events, [('expect', 'one'), ('expect', 'two'), ('cancel', 'one'), ('queue', 'two')])
        self.assertIsNone(notifications.remembered_prompt(dict(self.payload, prompt_id='one')))
        self.stop_entries(entries)
        self.assertEqual(self.events.count(('cancel', 'one')), 1)

    def unrecorded_fixture(self, first='question', second='question /mute'):
        transcript = self.root / 'transcript'
        anchor = dict(type='assistant', uuid='anchor', message={'content': 'previous reply'})
        transcript.write_text(json.dumps(anchor) + '\n')
        self.payload['transcript_path'] = str(transcript)
        self.submit(first)
        self.payload['prompt_id'] = 'two'
        self.submit(second)
        return [anchor, dict(type='user', uuid='new', parentUuid='anchor', promptId='two',
                             message={'content': second})]

    def test_unrecorded_submission_is_cancelled_after_muted_replacement(self):
        entries = self.unrecorded_fixture()
        self.stop_entries(entries)
        self.assertEqual(self.events, [('expect', 'one'), ('expect', 'two'), ('skip', 'two'),
                                       ('cancel', 'one'), ('skip', 'two')])
        self.assertIsNone(notifications.remembered_prompt(dict(self.payload, prompt_id='one')))
        self.assertFalse(self.sent)

    def test_unrecorded_cancellation_retries_even_after_current_cache_is_removed(self):
        entries = self.unrecorded_fixture()
        original = stop.notify
        with patch.object(stop, 'notify', side_effect=lambda action, *args, **kw:
                          False if action == 'cancel' else original(action, *args, **kw)):
            self.stop_entries(entries)
        self.assertIsNone(notifications.remembered_prompt(self.payload))
        self.assertEqual(notifications.remembered_prompt(dict(self.payload, prompt_id='one')), 'question')
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)

    def test_unrecorded_cleanup_waits_for_background_work(self):
        entries = self.unrecorded_fixture()
        self.stop_entries(entries, pending=('task',))
        self.assertNotIn(('cancel', 'one'), self.events)
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)

    def test_unrecorded_cleanup_requires_intact_checkpoint_and_written_current_prompt(self):
        entries = self.unrecorded_fixture()
        for snapshot in (entries[:1], entries[1:], list(reversed(entries))):
            self.stop_entries(snapshot)
            self.assertNotIn(('cancel', 'one'), self.events)

    def test_recorded_prompt_is_not_abandoned_by_absence_cleanup(self):
        entries = self.unrecorded_fixture()
        entries.insert(1, dict(type='user', uuid='old', parentUuid='anchor', promptId='one',
                               message={'content': 'question'}))
        entries[2]['parentUuid'] = 'old'
        self.stop_entries(entries)
        self.assertNotIn(('cancel', 'one'), self.events)

    def test_unrecorded_cleanup_is_scoped_to_session_and_transcript(self):
        entries = self.unrecorded_fixture()
        for change in ({'session_id': 'different'}, {'transcript_path': str(self.root/'different')}):
            self.assertEqual(notifications.unrecorded_prompts(
                dict(self.payload, background_tasks=[], **change), entries), [])
        for change in ({'TMUX_PANE': '%another'}, {'TMUX': '/another/socket,123,0'}):
            with patch.dict(os.environ, change):
                self.assertEqual(notifications.unrecorded_prompts(
                    dict(self.payload, background_tasks=[]), entries), [])

    def test_unrecorded_cleanup_ignores_background_wakeup_and_missing_task_list(self):
        entries = self.unrecorded_fixture()
        self.assertEqual(notifications.unrecorded_prompts(self.payload, entries), [])
        entries[-1]['message']['content'] = '<task-notification><task-id>task</task-id></task-notification>'
        self.stop_entries(entries)
        self.assertNotIn(('cancel', 'one'), self.events)

    def test_already_written_submission_does_not_capture_an_absence_checkpoint(self):
        entries = self.unrecorded_fixture()
        transcript = Path(self.payload['transcript_path'])
        transcript.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        self.submit('question /mute')
        data = json.loads(notifications.prompt_path(self.payload).read_text())
        self.assertIsNone(data['checkpoint'])

    def test_incomplete_transcript_does_not_capture_an_absence_checkpoint(self):
        entries = self.unrecorded_fixture()
        transcript = Path(self.payload['transcript_path'])
        transcript.write_text(json.dumps(entries[0])+'\n{"type":')
        self.payload['prompt_id'] = 'three'
        self.submit('question')
        data = json.loads(notifications.prompt_path(self.payload).read_text())
        self.assertIsNone(data['checkpoint'])

    def test_new_prompt_alone_is_not_proof_of_abandonment(self):
        self.stop_entries(self.abandoned_fixture())
        self.assertNotIn(('cancel', 'one'), self.events)

    def test_replaced_prompt_with_background_work_stays_pending(self):
        entries = self.abandoned_fixture(tasks=('task',), sibling=True, failed=True)
        self.stop_entries(entries)
        self.assertNotIn(('cancel', 'one'), self.events)

    def test_stopping_all_tasks_releases_their_original_prompt(self):
        self.stop_entries(self.abandoned_fixture(tasks=('task', 'nested')))
        self.assertIn(('cancel', 'one'), self.events)
        self.assertIn(('queue', 'two'), self.events)
        self.assertEqual(len(self.sent), 1)

    def test_failed_or_unmatched_task_stop_does_not_cancel(self):
        for kwargs in ({'failed': True}, {'unmatched': True}):
            self.events.clear()
            self.payload['prompt_id'] = 'one'
            self.stop_entries(self.abandoned_fixture(tasks=('task',), **kwargs))
            self.assertNotIn(('cancel', 'one'), self.events)

    def test_remaining_task_keeps_expectation_even_if_missing_from_payload(self):
        entries = self.abandoned_fixture(tasks=('first', 'second'))
        self.stop_entries(entries[:-2])
        self.assertNotIn(('cancel', 'one'), self.events)

    def test_task_reported_running_still_keeps_expectation(self):
        entries = self.abandoned_fixture(tasks=('task',))
        self.stop_entries(entries, pending=('task',))
        self.assertNotIn(('cancel', 'one'), self.events)

    def test_current_task_owner_is_not_cancelled(self):
        entries = self.abandoned_fixture(tasks=('task',))
        entries.append(dict(type='user', promptId='wakeup', message={'content':
            '<task-notification><task-id>task</task-id></task-notification>'}))
        self.payload['prompt_id'] = 'wakeup'
        self.stop_entries(entries)
        self.assertNotIn(('cancel', 'one'), self.events)
        self.assertIn(('queue', 'one'), self.events)

    def test_failed_cancellation_is_retried_on_next_stop(self):
        entries = self.abandoned_fixture(sibling=True)
        original = stop.notify
        with patch.object(stop, 'notify', side_effect=lambda action, *args, **kw:
                          False if action == 'cancel' else original(action, *args, **kw)):
            self.stop_entries(entries)
        self.assertEqual(notifications.remembered_prompt(dict(self.payload, prompt_id='one')), 'old question')
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)

    def attached_fixture(self, tasks=('task',)):
        self.submit('old question')
        entries = [dict(type='user', promptId='one', message={'content': 'old question'})]
        for task in tasks:
            entries.append(dict(type='user', message={'content': []},
                                toolUseResult={'backgroundTaskId': task}))
        self.payload['prompt_id'] = 'two'
        self.submit('new question')
        entries.append(dict(type='user', promptId='two', message={'content': 'new question'}))
        entries.append(task_attachment(tasks[-1]))
        return entries

    def test_attached_completion_retires_original_expectation_and_keeps_current_review(self):
        entries = self.attached_fixture()
        self.stop_entries(entries)
        self.assertEqual(self.events, [('expect', 'one'), ('expect', 'two'),
                                       ('cancel', 'one'), ('queue', 'two')])
        self.assertEqual(len(self.sent), 1)
        self.assertIn('USER:\nnew question', self.sent[0])
        self.assertIsNone(notifications.remembered_prompt(dict(self.payload, prompt_id='one')))
        self.stop_entries(entries)
        self.assertEqual(self.events.count(('cancel', 'one')), 1)
        self.assertEqual(len(self.sent), 1)

    def test_attached_completion_waits_for_every_original_task(self):
        entries = self.attached_fixture(tasks=('first', 'second'))
        self.stop_entries(entries)
        self.assertNotIn(('cancel', 'one'), self.events)
        entries.append(task_attachment('first', 'failed'))
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)

    def test_mixed_standalone_and_attached_completions_release_original(self):
        entries = self.attached_fixture(tasks=('first', 'second'))
        entries.insert(-2, dict(type='user', message={'content':
            task_attachment('first')['attachment']['prompt']}))
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)
        self.assertIn(('queue', 'two'), self.events)

    def test_attached_completion_does_not_override_running_task_payload(self):
        entries = self.attached_fixture()
        self.stop_entries(entries, pending=('task',))
        self.assertNotIn(('cancel', 'one'), self.events)
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)

    def test_attached_completion_of_current_owner_keeps_its_review(self):
        self.submit('question')
        entries = [dict(type='user', promptId='one', message={'content': 'question'}),
                   dict(type='user', message={'content': []}, toolUseResult={'backgroundTaskId': 'task'}),
                   task_attachment('task')]
        self.stop_entries(entries)
        self.assertEqual(self.events, [('expect', 'one'), ('queue', 'one')])

    def test_unknown_progress_or_untrusted_attachments_do_not_retire_reviews(self):
        entries = self.attached_fixture()
        missing_origin = task_attachment('task')
        missing_origin['attachment'].pop('origin')
        quoted = dict(type='user', message={'content': 'Example: ' + task_attachment('task')['attachment']['prompt']})
        queued_only = dict(task_attachment('task'), type='queue-operation')
        wrong_mode = task_attachment('task')
        wrong_mode['attachment']['commandMode'] = 'prompt'
        malformed = task_attachment('task')
        malformed['attachment']['prompt'] = None
        for attachment in (task_attachment('unknown'), task_attachment('task', 'running'),
                           task_attachment('task', ''), missing_origin, quoted, queued_only,
                           wrong_mode, malformed):
            with self.subTest(attachment=attachment):
                self.stop_entries(entries[:-1] + [attachment])
                self.assertNotIn(('cancel', 'one'), self.events)

    def test_attached_completion_keeps_nested_work_pending(self):
        entries = self.attached_fixture()
        # An earlier completion resumed the old prompt and launched more work.
        entries[-2:-2] = [dict(type='user', message={'content':
            task_attachment('task')['attachment']['prompt']}),
            dict(type='user', message={'content': []}, toolUseResult={'backgroundTaskId': 'nested'})]
        self.stop_entries(entries)
        self.assertNotIn(('cancel', 'one'), self.events)
        entries.append(task_attachment('nested', 'killed'))
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)

    def test_attached_completion_cancellation_retries_without_replaying_reply(self):
        entries = self.attached_fixture()
        original = stop.notify
        with patch.object(stop, 'notify', side_effect=lambda action, *args, **kw:
                          False if action == 'cancel' else original(action, *args, **kw)):
            self.stop_entries(entries)
        self.assertIsNotNone(notifications.remembered_prompt(dict(self.payload, prompt_id='one')))
        self.stop_entries(entries)
        self.assertIn(('cancel', 'one'), self.events)
        self.assertEqual(len(self.sent), 1)

    def monitor_fixture(self, event, attached=False):
        self.submit('watch the task')
        entries = [dict(type='user', promptId='one', message={'content':'watch the task'}),
                   dict(type='assistant', message={'content':[dict(type='tool_use', id='launch', name='Monitor')]}),
                   dict(type='user', message={'content':[dict(type='tool_result', tool_use_id='launch')]},
                        toolUseResult={'taskId':'monitor'})]
        self.payload['prompt_id'] = 'two'
        self.submit('next question')
        notice = ('<task-notification><task-id>monitor</task-id>'
                  '<summary>Monitor event: fixture</summary><event>'+event+'</event></task-notification>')
        if not attached:
            entries.append(dict(type='user', promptId='wake', origin={'kind':'task-notification'},
                                message={'content':notice}))
        entries.append(dict(type='user', promptId='two', message={'content':'next question'}))
        if attached:
            entry = task_attachment('monitor')
            entry['attachment']['prompt'] = notice
            entries.append(entry)
        return entries

    def test_monitor_expiration_is_terminal_in_both_delivery_formats(self):
        for attached in (False, True):
            for count, suffix in [('3 events', ''), ('1 event', ''),
                                  ('no events', ' — and widen the filter if silence was unexpected')]:
                with self.subTest(attached=attached, count=count):
                    self.payload['prompt_id'] = 'one'
                    self.events.clear()
                    event = f'[Monitor expired after 30m with {count} delivered. Re-arm it if you still need the watch{suffix}.]'
                    self.stop_entries(self.monitor_fixture(event, attached))
                    self.assertIn(('cancel','one'), self.events)

    def test_monitor_progress_quotes_and_unverified_launch_do_not_expire_review(self):
        expired = '[Monitor expired after 30m with 1 event delivered. Re-arm it if you still need the watch.]'
        for event, name, origin in [('still watching', 'Monitor', 'task-notification'),
                                     ('output: '+expired, 'Monitor', 'task-notification'),
                                     (expired, 'Bash', 'task-notification'),
                                     (expired, 'Monitor', 'human')]:
            with self.subTest(event=event, name=name, origin=origin):
                self.payload['prompt_id'] = 'one'
                self.events.clear()
                entries = self.monitor_fixture(event)
                entries[1]['message']['content'][0]['name'] = name
                entries[-2]['origin']['kind'] = origin
                self.stop_entries(entries)
                self.assertNotIn(('cancel','one'), self.events)

    def test_expired_monitor_rearm_keeps_new_work_pending(self):
        event = '[Monitor expired after 30m with no events delivered. Re-arm it if you still need the watch — and widen the filter if silence was unexpected.]'
        entries = self.monitor_fixture(event)
        entries.insert(-1, dict(type='user', message={'content':[]}, toolUseResult={'taskId':'rearmed'}))
        self.stop_entries(entries)
        self.assertNotIn(('cancel','one'), self.events)
        entries.append(task_attachment('rearmed'))
        self.stop_entries(entries)
        self.assertIn(('cancel','one'), self.events)

    def test_mixed_completed_and_stopped_tasks_retire_without_an_attachment(self):
        entries = self.abandoned_fixture(tasks=('finished','stopped'))
        # Remove the first TaskStop; that task ended normally before the next prompt.
        del entries[4:6]
        entries.insert(3, dict(type='user', promptId='wake', message={'content':
            task_attachment('finished')['attachment']['prompt']}))
        self.stop_entries(entries)
        self.assertIn(('cancel','one'), self.events)
        self.assertIn(('queue','two'), self.events)

    def test_batched_standalone_completions_retire_earlier_owner(self):
        self.submit('first task')
        entries = [dict(type='user', promptId='one', message={'content':'first task'}),
                   dict(type='user', message={'content':[]}, toolUseResult={'taskId':'first'})]
        self.payload['prompt_id'] = 'two'
        self.submit('second task')
        entries += [dict(type='user', promptId='two', message={'content':'second task'}),
                    dict(type='user', message={'content':[]}, toolUseResult={'taskId':'second'})]
        for task in ('first','second'):
            entries.append(dict(type='user', promptId='wake-'+task, message={'content':
                task_attachment(task)['attachment']['prompt']}))
        self.payload['prompt_id'] = 'wake-second'
        self.stop_entries(entries)
        self.assertIn(('cancel','one'), self.events)
        self.assertNotIn(('cancel','two'), self.events)
        self.assertIn(('queue','two'), self.events)

    def test_compaction_keeps_one_owner_and_original_prompt_for_all_tasks(self):
        self.submit('original request')
        entries = [dict(type='user', promptId='one', message={'content':'original request'}),
                   dict(type='user', message={'content':[]}, toolUseResult={'taskId':'first'}),
                   dict(type='user', promptId='one', isCompactSummary=True, message={'content':'summary'}),
                   dict(type='user', message={'content':[]}, toolUseResult={'taskId':'second'}),
                   dict(type='user', promptId='wake', message={'content':
                        task_attachment('second')['attachment']['prompt']})]
        owner, tasks, _ = stop.exchange_history(entries)
        self.assertEqual(owner, 0)
        self.assertEqual(tasks[owner], {'first','second'})
        self.payload['prompt_id'] = 'wake'
        self.stop_entries(entries, pending=('first',))
        self.assertFalse(self.sent)
        entries.append(task_attachment('first'))
        self.stop_entries(entries)
        self.assertIn('USER:\noriginal request', self.sent[0])

    def queued_fixture(self, attached=False, text='queued question'):
        entries = [dict(type='user', uuid='root', promptId='root', message={'content':'original work'}),
                   dict(type='user', uuid='launch', message={'content':[]}, toolUseResult={'taskId':'task'}),
                   dict(type='user', uuid='anchor', promptId='one', origin={'kind':'task-notification'},
                        message={'content':task_attachment('task')['attachment']['prompt']})]
        transcript = self.root/'transcript'
        transcript.write_text(''.join(json.dumps(e)+'\n' for e in entries))
        self.payload.update(prompt_id='one', transcript_path=str(transcript))
        self.submit(text)
        entries.append(dict(type='queue-operation', operation='enqueue', content=text))
        if attached:
            entries.extend([
                dict(type='queue-operation', operation='remove', reason='absorbed_mid_turn',
                     commandUuid='source', deliveryId='receipt', content=text),
                dict(type='attachment', attachment=dict(type='queued_command', commandMode='prompt',
                     origin={'kind':'human'}, source_uuid='source', delivery_id='receipt', prompt=text)),
            ])
        else:
            entries.append(dict(type='user', uuid='delivered', promptId='delivered',
                                origin={'kind':'human'}, message={'content':text}))
            self.payload['prompt_id'] = 'delivered'
        return entries

    def test_queued_prompt_captures_checkpoint_despite_reused_wakeup_id(self):
        self.queued_fixture()
        record = json.loads(notifications.prompt_path(dict(self.payload,prompt_id='one')).read_text())
        self.assertEqual(record['checkpoint']['anchor'], 'anchor')

    def test_queued_prompt_new_id_retires_old_reservation_and_reviews_actual_turn(self):
        entries = self.queued_fixture()
        self.stop_entries(entries)
        self.assertEqual(self.events, [('expect','one'), ('cancel','one'), ('queue','delivered')])
        self.assertIn('USER:\nqueued question', self.sent[0])
        self.stop_entries(entries)
        self.assertEqual(self.events.count(('cancel','one')), 1)
        self.assertEqual(len(self.sent), 1)

    def test_queued_human_attachment_clears_alias_at_receiving_stop(self):
        entries = self.queued_fixture(attached=True)
        self.stop_entries(entries)
        self.assertIn(('cancel','one'), self.events)
        self.assertIn(('queue','root'), self.events)

    def test_queued_alias_waits_for_work_and_retries_failed_cancellation(self):
        entries = self.queued_fixture(attached=True)
        self.stop_entries(entries, pending=('task',))
        self.assertNotIn(('cancel','one'), self.events)
        original = stop.notify
        with patch.object(stop,'notify',side_effect=lambda action,*a,**kw:
                          False if action=='cancel' else original(action,*a,**kw)):
            self.stop_entries(entries)
        self.assertIsNotNone(notifications.remembered_prompt(dict(self.payload,prompt_id='one')))
        self.stop_entries(entries)
        self.assertEqual(self.events.count(('cancel','one')), 1)
        self.assertEqual(len(self.sent), 1)

    def test_queued_alias_requires_exact_delivery_and_intact_scope(self):
        entries = self.queued_fixture(attached=True)
        for change in ('queue-only','receipt-only','attachment-only','wrong-receipt','wrong-origin',
                       'wrong-text','missing-anchor','stale-stop','unknown-work','other-pane',
                       'other-session','other-transcript'):
            with self.subTest(change=change):
                snapshot = json.loads(json.dumps(entries))
                payload = dict(self.payload, background_tasks=[])
                env = {}
                if change=='queue-only': snapshot=snapshot[:-2]
                if change=='receipt-only': snapshot=snapshot[:-1]
                if change=='attachment-only': del snapshot[-2]
                if change=='wrong-receipt': snapshot[-1]['attachment']['delivery_id']='other'
                if change=='wrong-origin': snapshot[-1]['attachment']['origin']['kind']='task-notification'
                if change=='wrong-text': snapshot[-1]['attachment']['prompt']='different'
                if change=='missing-anchor': del snapshot[2]
                if change=='stale-stop': payload['prompt_id']='previous'
                if change=='unknown-work': payload.pop('background_tasks')
                if change=='other-pane': env['TMUX_PANE']='%other'
                if change=='other-session': payload['session_id']='other'
                if change=='other-transcript': payload['transcript_path']=str(self.root/'other')
                with patch.dict(os.environ,env):
                    self.assertEqual(notifications.resolved_queued_prompts(payload,snapshot), [])

    def test_queued_only_submission_is_not_cancelled_by_later_human_stop(self):
        entries = self.queued_fixture(attached=True)[:-2]
        entries.append(dict(type='user', promptId='later', uuid='later',
                            message={'content':'unrelated question'}))
        self.payload['prompt_id']='later'
        self.stop_entries(entries)
        self.assertNotIn(('cancel','one'), self.events)

    def test_identical_queued_prompts_require_distinct_deliveries(self):
        entries = self.queued_fixture()
        self.payload['prompt_id']='second-alias'
        self.submit('queued question')
        self.payload['prompt_id']='delivered'
        self.stop_entries(entries)
        self.assertNotIn(('cancel','one'), self.events)
        self.assertNotIn(('cancel','second-alias'), self.events)
        entries.append(dict(entries[-1], uuid='another', promptId='another'))
        self.payload['prompt_id']='another'
        self.stop_entries(entries)
        self.assertIn(('cancel','one'), self.events)
        self.assertIn(('cancel','second-alias'), self.events)

    def test_repeated_attachment_receipt_cannot_clear_two_queued_prompts(self):
        entries = self.queued_fixture(attached=True)
        self.payload['prompt_id']='second-alias'
        self.submit('queued question')
        self.payload['prompt_id']='one'
        entries.append(json.loads(json.dumps(entries[-1])))
        self.stop_entries(entries)
        self.assertNotIn(('cancel','one'), self.events)
        self.assertNotIn(('cancel','second-alias'), self.events)

    def test_ambiguous_old_checkpoint_keeps_identical_queued_prompt_pending(self):
        entries = self.queued_fixture()
        self.payload['prompt_id']='second-alias'
        self.submit('queued question')
        path=notifications.prompt_path(self.payload)
        record=json.loads(path.read_text());record['checkpoint']=None
        path.write_text(json.dumps(record))
        self.payload['prompt_id']='delivered'
        self.stop_entries(entries)
        self.assertNotIn(('cancel','one'), self.events)
        self.assertNotIn(('cancel','second-alias'), self.events)


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
