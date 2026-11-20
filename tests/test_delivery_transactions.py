"""Delivery stages, source ownership, and notification refusal semantics."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
import kibitz_hook_common as common
import kibitz_hook_user_prompt_submit as prompt
import kibitz_hook_stop as stop
import kibitz_notifications as notifications
import kibitz_codex_stop as codex


class DeliveryStagesTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.before = dict(identity=('10', '/dev/pts/1', 11, 'new', 'codex'),
                           agent='codex', composer='› original', busy=False)
        self.after = dict(self.before, composer='› pasted')
        for name, kw in (
            ('CACHE_DIR', dict(new=Path(temporary.name))),
            ('RUNTIME_DIR', dict(new=Path(temporary.name) / 'runtime')),
            ('source_context', dict(return_value={'pane': '%1'})),
            ('delivery_target', dict(side_effect=[self.before, self.before, self.after])),
            ('delivery_tmux', {}), ('delivery_command', {}),
            ('reviewer_busy', dict(return_value=False)), ('log', {}),
        ):
            p = patch.object(common, name, **kw)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.reserved = []

    def deliver(self, **kw):
        return common.forward_prompt('%2', 'message', before_paste=lambda: self.reserved.append(True), **kw)

    def check_failure(self, stage):
        with self.assertRaises(common.DeliveryRefused) as caught:
            self.deliver()
        self.assertEqual(caught.exception.stage, stage)
        action = {'not_pasted': 'undelivered', 'pasted': 'unsubmitted'}.get(stage, 'fail')
        self.assertEqual(common.delivery_failure_action(caught.exception), action)
        return caught.exception

    def test_preflight_refusal_does_not_reserve_or_paste(self):
        self.delivery_target.side_effect = common.DeliveryRefused('waiting')
        self.check_failure('not_pasted')
        self.assertFalse(self.reserved)
        self.delivery_command.assert_not_called()

    def test_native_paste_guard_refusal_is_known_not_pasted(self):
        self.delivery_command.side_effect = common.DeliveryRefused('changed')
        self.check_failure('not_pasted')
        self.assertEqual(self.reserved, [True])

    def test_paste_timeout_is_ambiguous(self):
        self.delivery_command.side_effect = subprocess.TimeoutExpired('tmux', 5)
        self.check_failure('paste_unknown')
        self.assertEqual(self.delivery_command.call_count, 1)

    def test_submit_timeout_is_ambiguous(self):
        self.delivery_command.side_effect = [None, subprocess.TimeoutExpired('tmux', 5)]
        error = self.check_failure('submission_unknown')
        self.assertIn('delivery unconfirmed', common.report_delivery_failure(error))

    def test_submit_guard_refusal_warns_that_text_was_already_pasted(self):
        self.delivery_command.side_effect = [None, common.DeliveryRefused('changed')]
        error = self.check_failure('pasted')
        message = common.report_delivery_failure(error)
        self.assertIn('text pasted, but no submit key sent', message)
        self.assertNotIn('not sent', message)
        args = self.delivery_tmux.call_args.args
        self.assertIn('@kibitz-delivery-error', args)
        self.assertIn('display-message', args)

    def test_queue_happens_between_preflight_and_paste(self):
        self.delivery_command.side_effect = lambda *args: self.assertEqual(self.reserved, [True])
        self.deliver()
        self.assertTrue(self.delivery_command.call_args.args[1].endswith(' Enter'))

    def test_busy_codex_gets_tab_but_force_only_changes_the_submit_key(self):
        self.after['busy'] = True
        self.deliver()
        self.assertTrue(self.delivery_command.call_args.args[1].endswith(' Tab'))
        self.delivery_target.side_effect = [self.before, self.before, self.after]
        self.reserved.clear()
        self.deliver(force=True)
        self.assertTrue(self.delivery_command.call_args.args[1].endswith(' Enter'))
        self.delivery_target.side_effect = common.DeliveryRefused('waiting')
        with self.assertRaises(common.DeliveryRefused):
            self.deliver(force=True)

    def test_hidden_cursor_after_paste_is_retried_without_pasting_again(self):
        self.delivery_target.side_effect = [self.before, self.before,
            common.DeliveryRefused('redraw', transient=True), self.after]
        self.deliver()
        self.assertEqual(self.delivery_command.call_count, 2)
        self.assertTrue(self.delivery_command.call_args.args[1].endswith(' Enter'))


    def test_native_paste_redraw_rechecks_without_reserving_twice(self):
        self.delivery_target.side_effect = [self.before, self.before, self.before, self.after]
        self.delivery_command.side_effect = [common.DeliveryRefused('redraw', transient=True), None, None]
        self.deliver()
        self.assertEqual(self.reserved, [True])
        self.assertEqual(self.delivery_command.call_count, 3)

    def test_native_submit_redraw_is_retried_but_timeout_is_not(self):
        self.delivery_target.side_effect = [self.before, self.before, self.after, self.after]
        self.delivery_command.side_effect = [None, common.DeliveryRefused('redraw', transient=True), None]
        self.deliver()
        self.assertEqual(self.delivery_command.call_count, 3)
        self.assertEqual(sum(call.args[1].startswith('paste-buffer')
                             for call in self.delivery_command.call_args_list), 1)

    def test_reservation_cannot_authorize_a_later_question(self):
        self.delivery_target.side_effect = [self.before, common.DeliveryRefused('question')]
        self.check_failure('not_pasted')
        self.assertTrue(self.reserved)
        self.delivery_command.assert_not_called()

    def test_permanent_question_after_paste_does_not_retry_or_send_a_key(self):
        self.delivery_target.side_effect = [self.before, self.before, common.DeliveryRefused('question')]
        self.check_failure('pasted')
        self.assertEqual(self.delivery_command.call_count, 1)

    def test_error_diagnostic_is_literal_and_persisted(self):
        text = 'literal #{pane_id}, } and quotes'
        message = common.report_delivery_failure(common.DeliveryRefused(text))
        self.assertIn(text, message)
        self.assertNotIn('Retry when', message)
        self.assertIn('@kibitz-delivery-error', self.delivery_tmux.call_args.args)


class SourceOwnershipTest(unittest.TestCase):
    def setUp(self):
        for obj, name, kw in (
            (os, 'getpid', dict(return_value=50)),
            (common, '_SOURCE_CONTEXT', dict(new=None)),
            (common, 'delivery_tmux', dict(return_value='10\t/dev/pts/1\t0\n')),
            (common, 'log', {}),
        ):
            p = patch.object(obj, name, **kw)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, TMUX='/fixture/tmux,42,0', TMUX_PANE='%1')
        p.start(); self.addCleanup(p.stop)

    def process_table(self, parent=11):
        return {50: dict(parent=parent, command='python3', started='new'),
                11: dict(parent=10, command='codex', started='new'),
                10: dict(parent=1, command='bash', started='old')}

    def test_only_a_descendant_can_claim_the_source(self):
        with patch.object(common.processes, 'ancestors', return_value=self.process_table()):
            context = common.source_context()
        self.assertEqual(context['pane_pid'], '10')
        self.assertIn(11, context['ancestors'])

    def test_daemon_with_another_panes_environment_cannot_claim_it(self):
        with patch.object(common.processes, 'ancestors', return_value=self.process_table(parent=99)):
            with self.assertRaisesRegex(common.DeliveryRefused, 'does not belong'):
                common.source_context()


    def test_one_timed_out_source_read_is_retried(self):
        self.delivery_tmux.side_effect = [subprocess.TimeoutExpired('tmux', 5), '10\t/dev/pts/1\t0\n']
        with patch.object(common.processes, 'ancestors', return_value=self.process_table()):
            self.assertTrue(common.valid_source())
        self.assertEqual(self.delivery_tmux.call_count, 2)

    def test_root_owned_pane_parent_need_not_be_read(self):
        records = self.process_table()
        del records[10]
        with patch.object(common.processes, 'ancestors', return_value=records):
            self.assertEqual(common.source_context()['pane_pid'], '10')

    def test_unvalidated_source_cannot_write_handoffs_or_rollout_or_route(self):
        refusal = common.DeliveryRefused('wrong pane')
        with patch.object(common, 'source_context', side_effect=refusal), \
             patch.object(common.subprocess, 'run') as run, \
             patch.object(Path, 'write_text') as write, \
             patch.object(prompt, 'resolve_counterpart') as pair, \
             patch.object(stop, 'load_entries') as entries:
            payload = dict(session_id='s', prompt_id='p', prompt='hello /tee', transcript_path='/fixture')
            notifications.notify('queue', payload, '%2', 'hello')
            notifications.remember_prompt(payload)
            codex.record_rollout(payload)
            self.assertEqual(prompt.handle_prompt(payload), 0)
            with patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))):
                self.assertEqual(stop.main(), 0)
            self.assertIsNone(common.resolve_reviewer())
            self.assertIsNone(common.resolve_counterpart('codex'))
            for mock in (run, write, pair, entries):
                mock.assert_not_called()


class RefusedHandoffTest(unittest.TestCase):
    def test_prompt_preflight_refusal_retires_only_undelivered_work(self):
        with patch.object(common, 'valid_source', return_value=True), \
             patch.object(prompt, 'resolve_counterpart', return_value=('%2', 'claude', True)), \
             patch.object(prompt, 'notify') as notify, \
             patch.object(prompt, 'report_delivery_failure'), \
             patch.object(prompt, 'forward_prompt', side_effect=common.DeliveryRefused('waiting')):
            prompt.handle_prompt(dict(session_id='s', turn_id='t', prompt='hello /tee'), 'codex')
            self.assertEqual([call.args[0] for call in notify.call_args_list], ['undelivered'])


if __name__ == '__main__':
    unittest.main()
