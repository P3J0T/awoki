"""Offline recovery journeys: durable state is navigation, never semantic proof."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import harness_core as core
import opencode_events
import project_workspace
import work_ledger


class GoalRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.paths = core.HarnessPaths(self.root, self.root / 'global')
        self.sid = 'recovery-session'

    def attach(self):
        core.project_create('demo', session_id=self.sid, paths=self.paths)
        return project_workspace.paths_for(self.root, 'demo')

    def save_todos(self, todos=None):
        return work_ledger.sync_todos(self.root, self.sid, todos if todos is not None else [
            {'content': 'Inspect the callback', 'status': 'in_progress', 'priority': 'high'}])

    def status(self):
        return core.session_work_status(session_id=self.sid, paths=self.paths)

    def test_no_goal_is_valid_and_does_not_create_project(self):
        self.save_todos()
        state = self.status()
        self.assertEqual(state['goal_recovery']['reason'], 'unattached_ad_hoc')
        self.assertEqual(state['goal_recovery']['status'], 'none')
        self.assertTrue(state['recovery']['state_available'])
        self.assertFalse(project_workspace.current_project_id(self.root, session_id=self.sid))

    def test_empty_todos_do_not_erase_goal_and_exact_recovery_uses_no_models(self):
        self.attach()
        saved = core.project_capture('Trace the callback', kind='direction', details='Only the web repository; report unknown external behavior.', name='demo', paths=self.paths)
        self.save_todos([])
        with mock.patch.object(core.rag_backend, 'search_qdrant') as remote:
            state = self.status()
        remote.assert_not_called()
        self.assertEqual(state['goal_recovery']['directions'][0]['record_id'], saved['id'])
        self.assertEqual(state['todos'], [])
        context = opencode_events.compaction_context(self.root, self.sid)['context']
        self.assertIn(saved['id'], context)
        self.assertIn('No mirrored TODOs', context)

    def test_corrupt_work_is_unknown_and_not_overwritten_by_update(self):
        self.save_todos()
        path = work_ledger._path(self.root, self.sid)
        path.write_text('{broken')
        state = self.status()
        self.assertEqual(state['status'], 'unknown')
        self.assertFalse(state['recovery']['state_available'])
        with self.assertRaises(ValueError):
            self.save_todos([])
        self.assertEqual(path.read_text(), '{broken')
        self.assertIn('Work recovery unavailable', work_ledger.compact_context(self.root, self.sid))

    def test_corrupt_goal_is_not_no_goal(self):
        pp = self.attach()
        pp.continuity.write_text('{broken')
        state = self.status()
        self.assertEqual(state['goal_recovery']['status'], 'unknown')
        self.assertTrue(state['recovery']['needs_reconciliation'])

    def test_new_direction_keeps_only_current_exact_pointer(self):
        self.attach()
        old = core.project_capture('Trace callbacks', kind='direction', name='demo', paths=self.paths)
        new = core.project_capture('Review docs instead', kind='direction', supersedes=[old['id']], name='demo', paths=self.paths)
        state = self.status()
        self.assertEqual(state['goal_recovery']['status'], 'saved')
        self.assertEqual([r['record_id'] for r in state['goal_recovery']['directions']], [new['id']])
        opened = core.project_open('demo', session_id=self.sid, paths=self.paths)
        self.assertEqual(opened['goal_recovery'], state['goal_recovery'])

    def test_project_switch_marks_old_work_for_reconciliation(self):
        self.attach()
        self.save_todos()
        core.project_create('other', session_id=self.sid, paths=self.paths)
        state = self.status()
        self.assertFalse(state['scope_matches'])
        self.assertTrue(state['recovery']['needs_reconciliation'])
        self.assertEqual(state['current_project_id'], 'other')
        self.assertEqual(state['project_id'], 'demo')
        self.save_todos()
        replayed = self.status()
        self.assertFalse(replayed['scope_matches'])
        self.assertTrue(replayed['recovery']['needs_reconciliation'])

    def test_corrupt_attachment_is_not_unattached_exploration(self):
        self.attach()
        project_workspace.session_state_path(self.root, self.sid).write_text('{broken')
        state = self.status()
        self.assertEqual(state['goal_recovery']['status'], 'unknown')
        self.assertEqual(state['goal_recovery']['reason'], 'session_scope_unavailable')
        self.assertFalse(state['recovery']['state_available'])
        self.assertEqual(self.save_todos()['status'], 'rejected')

    def test_malformed_stored_todos_and_generations_are_unknown(self):
        self.save_todos()
        path = work_ledger._path(self.root, self.sid)
        for bad in ({'todos': [42]}, {'todos': [], 'compaction_generation': 'garbage'},
                    {'todos': [], 'active_references': [None]}):
            path.write_text(json.dumps(bad))
            self.assertEqual(self.status()['status'], 'unknown')
            self.assertIn('Work recovery unavailable', work_ledger.compact_context(self.root, self.sid))

    def test_malformed_todos_preserve_last_valid_list_but_empty_clears(self):
        self.save_todos()
        before = work_ledger._path(self.root, self.sid).read_bytes()
        for bad in (None, {}, [None], [{'content': 42}], [{'content': ''}]):
            with self.subTest(bad=bad):
                self.assertEqual(work_ledger.sync_todos(self.root, self.sid, bad)['status'], 'rejected')
                self.assertEqual(work_ledger._path(self.root, self.sid).read_bytes(), before)
        self.save_todos([])
        self.assertEqual(self.status()['todos'], [])

    def test_malformed_cli_payload_does_not_clear_existing_work(self):
        self.save_todos()
        env = dict(os.environ, AWOKI_ROOT=str(self.root))
        for data in ('not json', '{}', '{"todos":null}'):
            result = subprocess.run([sys.executable, str(Path(opencode_events.__file__)), 'todo-sync', '--session-id', self.sid], input=data, text=True, capture_output=True, env=env, check=True)
            self.assertEqual(json.loads(result.stdout)['status'], 'rejected')
        self.assertEqual(len(self.status()['todos']), 1)

    def test_long_todo_retains_late_record_ref_and_reports_omissions(self):
        ref = 'cont_20260929_abcdef012345'
        self.save_todos([{'content': 'x' * 850 + ' ' + ref}] * 65)
        state = self.status()
        self.assertTrue(state['todos'][0]['content_truncated'])
        self.assertEqual(state['todos'][0]['record_refs'], [ref])
        self.assertEqual(state['todos_omitted'], 1)
        context = work_ledger.compact_context(self.root, self.sid, max_chars=1800)
        self.assertLessEqual(len(context), 1800)
        self.assertIn(ref, context)
        self.assertIn('TODOs omitted:', context)

    def test_compaction_with_no_todos_still_routes_recovery(self):
        opencode_events.mark_compacted(self.root, self.sid)
        context = work_ledger.compact_context(self.root, self.sid)
        self.assertIn('session_work_status once', context)
        self.assertIn('compaction_generation: 1', context)
        self.assertIn('No mirrored TODOs', context)

    def test_reference_extraction_never_reintroduces_redacted_value(self):
        fake = 'cont_' + 'a' * 40
        self.save_todos([{'content': 'Authorization: Bearer ' + fake, 'record_refs': [fake]}])
        self.assertNotIn(fake, json.dumps(self.status()))
        self.assertNotIn(fake, work_ledger.compact_context(self.root, self.sid))
        # Redact before storage truncation even if the credential spans the limit.
        self.save_todos([{'content': 'x' * 780 + ' Authorization: Bearer ' + fake}])
        self.assertEqual(self.status()['todos'][0]['record_refs'], [])

    def test_oversized_reference_does_not_bypass_storage_bound(self):
        self.save_todos([{'content': 'cont_' + 'a' * 20_000}])
        self.assertEqual(self.status()['todos'][0]['record_refs'], [])
        self.assertLess(work_ledger._path(self.root, self.sid).stat().st_size, 4000)

    def test_bridge_truncation_and_omissions_remain_visible(self):
        result = work_ledger.sync_todos(self.root, self.sid,
            [{'content': 'short received text', 'content_truncated': True}], todos_omitted=4)
        self.assertEqual(result['status'], 'saved')
        self.assertTrue(self.status()['todos'][0]['content_truncated'])
        self.assertEqual(self.status()['todos_omitted'], 4)

    def test_task_completion_preserves_refs_and_requires_resolved_steps(self):
        self.attach()
        first = core.project_task_checkpoint('Review callback', name='demo', remaining_steps=['Inspect callback'], related_refs=['reports/callback.md'], paths=self.paths)
        result = core.project_task_finalize(task_id=first['task_id'], name='demo', paths=self.paths)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(core.project_task_status(first['task_id'], name='demo', paths=self.paths)['task_status'], 'running')
        core.project_task_checkpoint('Review callback', task_id=first['task_id'], name='demo', completed_steps=['Inspect callback'], remaining_steps=[], related_refs=['reports/callback.md'], paths=self.paths)
        result = core.project_task_finalize(task_id=first['task_id'], name='demo', paths=self.paths)
        self.assertEqual(result['status'], 'finalized')
        final = core.project_task_status(first['task_id'], name='demo', paths=self.paths)
        self.assertEqual(final['task_status'], 'done')
        self.assertEqual(final['completed_steps'], ['Inspect callback'])
        self.assertEqual(final['related_refs'], ['reports/callback.md'])

    def test_completion_checkpoint_cannot_bypass_unresolved_steps(self):
        self.attach()
        result = core.project_task_checkpoint('Review callback', name='demo', status='done',
            remaining_steps=['Inspect callback'], paths=self.paths)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(core.project_task_status(name='demo', paths=self.paths)['status'], 'none')

    def test_corrupt_latest_task_does_not_fall_back_to_older_completion(self):
        pp = self.attach()
        task = core.project_task_checkpoint('Review callback', name='demo', remaining_steps=[], paths=self.paths)
        with pp.continuity.open('a') as out:
            out.write('{broken latest unresolved checkpoint\n')
        result = core.project_task_finalize(task_id=task['task_id'], name='demo', paths=self.paths)
        self.assertEqual(result['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
