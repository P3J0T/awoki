"""The lightweight procedure uses existing memory, not a new checkpoint store.

These checks exercise persistence/routing, not a model's semantic judgment.
"""
from pathlib import Path
import unittest
from unittest import mock

import harness_core as core
import project_workspace
import test_memory_recall


ROOT = Path(__file__).resolve().parents[2]


class InvestigationCheckpointTests(unittest.TestCase):
    setUp = test_memory_recall.MemoryRecallTests.setUp
    save = test_memory_recall.MemoryRecallTests.save
    get = test_memory_recall.MemoryRecallTests.get

    def test_entry_points_route_plain_checkpoint_without_formal_task(self):
        skill = (ROOT / '.opencode/skills/project-continuity/SKILL.md').read_text()
        command = (ROOT / '.opencode/commands/project.md').read_text()
        policy = (ROOT / '.harness/AGENT_CORE.md').read_text()
        self.assertIn('Lightweight investigation checkpoint', skill)
        self.assertIn('checkpoint this investigation', command)
        self.assertIn('no formal task', command)
        self.assertIn('project-continuity', policy)
        self.assertLessEqual(len(policy), 3000)
        for phrase in ('at initial capture', 'Rejected lead:', 'state="closed"',
                       'based_on=[cont_...]', 'record_ids', 'newest user direction',
                       'partial/rejected', 'dated snapshots', 'before a planned pause',
                       'Never edit', 'SITUATION.md', 'after every tool call'):
            self.assertIn(phrase, skill)

    def test_checkpoint_retains_observation_caveats_and_next_check_on_exact_resume(self):
        observation = self.save('Router delegates to a callback', kind='observation',
            sources=[{'repo': 'web', 'path': 'router.ts'}], confidence='low',
            uncertainty=['Callback authorization was not inspected.'])
        before = self.pp.continuity.read_bytes()
        checkpoint = self.save('Checkpoint: callback boundary — behavior inside callback unknown',
            kind='reflection', tags=['investigation-checkpoint', 'callback'],
            details='Question/scope: web router. Established: callback invoked. '
                    'Unknown: callback authorization. Leads: unresolved callback implementation.',
            likely_continuation='Inspect the configured callback implementation.',
            based_on=[observation['id']])
        self.assertEqual(checkpoint['status'], 'captured')
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))
        self.assertEqual(checkpoint['sources'], observation['sources'])
        self.assertEqual(checkpoint['uncertainty'], observation['uncertainty'])
        self.assertEqual(checkpoint['confidence'], 'low')
        # Reconstruct paths as a new caller would; exact read has no semantic backend.
        paths = core.HarnessPaths(self.root, self.root / 'global')
        with mock.patch.object(core.rag_backend, 'search_qdrant') as remote:
            recalled = core.project_search(name='demo', record_ids=[checkpoint['id']], paths=paths)['records'][0]
        remote.assert_not_called()
        self.assertEqual(recalled['details'], checkpoint['details'])
        self.assertEqual(recalled['likely_continuation'], checkpoint['likely_continuation'])
        self.assertEqual(recalled['qualifications']['uncertainty'], observation['uncertainty'])
        self.assertFalse(recalled['source_freshness']['claim_verified'])

    def test_rejected_lead_is_retired_and_not_a_continuation(self):
        lead = self.save('Maybe the guarded route ignores the check', kind='hypothesis',
            likely_continuation='Check whether the guarded route ignores authorization.')
        before = self.pp.continuity.read_bytes()
        rejected = self.save('Rejected lead: guarded route does check the result',
            kind='correction', supersedes=[lead['id']], state='closed',
            sources=[{'repo': 'web', 'path': 'router.ts'}],
            uncertainty=['Deployed behavior was not tested.'])
        self.assertEqual(rejected['status'], 'captured')
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))
        records = project_workspace.continuity_records(self.pp)
        self.assertNotIn(lead['id'], [r['id'] for r in records])
        self.assertNotIn(lead['likely_continuation'], project_workspace._continuations(records, self.pp))
        restored = self.get([rejected['id']])['records'][0]
        self.assertEqual(restored['state'], 'closed')
        self.assertEqual(restored['supersedes'], [lead['id']])
        self.assertIn('Rejected lead:', self.pp.handoff.read_text())

    def test_new_direction_precedes_old_checkpoint_suggestion(self):
        checkpoint = self.save('Checkpoint: callback boundary', kind='reflection',
            tags=['investigation-checkpoint', 'callback'],
            likely_continuation='Inspect callback authorization.')
        direction = self.save('Review documentation instead; do not inspect callback now.', kind='direction')
        choices = project_workspace._continuations(project_workspace.continuity_records(self.pp), self.pp)
        self.assertEqual(choices[0], direction['summary'])
        self.assertEqual(self.get([checkpoint['id']])['records'][0]['likely_continuation'],
                         checkpoint['likely_continuation'])

    def test_checkpoint_correction_preserves_history_without_silent_descendant_rewrite(self):
        parent = self.save('Callback not inspected', uncertainty=['Callback authorization unknown.'])
        checkpoint = self.save('Checkpoint: callback unknown', kind='reflection', based_on=[parent['id']])
        fixed = self.save('Callback source inspected; execution still untested', kind='correction',
            supersedes=[parent['id']], sources=[{'repo': 'web', 'path': 'callback.ts'}],
            uncertainty=['Runtime execution not observed.'])
        # Snapshot semantics: a parent correction is not a rewrite of its descendants.
        self.assertEqual(self.get([checkpoint['id']])['records'][0]['uncertainty'], parent['uncertainty'])
        before = self.pp.continuity.read_bytes()
        replacement = self.save('Checkpoint corrected: callback source inspected; runtime unknown',
            kind='correction', supersedes=[checkpoint['id']], tags=['investigation-checkpoint', 'callback'],
            sources=fixed['sources'], uncertainty=fixed['uncertainty'])
        self.assertEqual(replacement['status'], 'captured')
        self.assertTrue(self.pp.continuity.read_bytes().startswith(before))
        ids = [r['id'] for r in project_workspace.continuity_records(self.pp)]
        self.assertNotIn(checkpoint['id'], ids)
        self.assertIn(replacement['id'], ids)

    def test_casual_note_still_needs_no_checkpoint_fields(self):
        saved = self.save('Use concise headings')
        self.assertEqual(saved['status'], 'captured')
        self.assertEqual(saved['kind'], 'observation')
        self.assertEqual(saved['sources'], [])
        self.assertEqual(saved['uncertainty'], [])
        self.assertFalse(saved.get('likely_continuation'))

    def test_mixed_batch_rejection_preserves_journal_and_explains_reference_recovery(self):
        before = self.pp.continuity.read_bytes()
        rejected = self.save('', items=[{'summary': 'Source observation'}], evidence_refs=['ev_example'])
        self.assertEqual(rejected['status'], 'rejected')
        self.assertEqual(rejected['written'], 0)
        self.assertEqual(self.pp.continuity.read_bytes(), before)
        for phrase in ('INSIDE', 'Do not drop references or caveats', 'separate single notes'):
            self.assertIn(phrase, rejected['reason'])
