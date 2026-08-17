from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_workspace
from harness_core import HarnessPaths, project_create, project_resume, project_status, project_pending, project_mark_pending, save_project_fact, save_finding, recall_context, index_project


class ProjectWorkspaceTests(unittest.TestCase):
    def test_generated_project_agents_boilerplate_is_not_kept_but_user_rules_are_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            pp = project_workspace.paths_for(root, 'asd')
            agents = pp.project_dir / 'AGENTS.md'
            self.assertFalse(agents.exists())

            legacy = (
                '# Project Rules: asd\n\n'
                'Project-local knowledge overrides global assumptions. '
                'User direction overrides suggested continuation.\n'
            )
            agents.write_text(legacy, encoding='utf-8')
            project_workspace.ensure_project_layout(root, 'asd')
            self.assertFalse(agents.exists())

            agents.write_text('# Project Rules\n\nDo not touch production.\n', encoding='utf-8')
            project_workspace.ensure_project_layout(root, 'asd')
            self.assertTrue(agents.exists())
            self.assertIn('Do not touch production', agents.read_text(encoding='utf-8'))

    def test_project_memory_freshness_is_explicitly_scoped_away_from_code_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            freshness = project_workspace.project_index_freshness(root, 'asd')
            self.assertIn('project_memory_index_current', freshness)
            self.assertEqual(freshness['fresh'], freshness['project_memory_index_current'])
            self.assertEqual(freshness['freshness_scope'], 'project_memory_general_rag_projection')
            self.assertEqual(freshness['does_not_describe'], 'structural_code_index_freshness')

    def test_create_resume_pending_and_mark(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '.harness' / 'memory').mkdir(parents=True)
            (root / '.opencode' / 'skills').mkdir(parents=True)
            paths = HarnessPaths(root=root, global_root=root / '.global')
            created = project_create('asd', paths=paths)
            self.assertEqual(created['status'], 'created')
            self.assertTrue((root / 'workspace' / 'projects' / 'asd' / 'SITUATION.md').exists())
            self.assertTrue((root / 'workspace' / 'projects' / 'asd' / 'HANDOFF.md').exists())
            pend = project_pending('asd', 'Review auth', 'Read auth-cookies.md', paths=paths)
            self.assertEqual(pend['status'], 'queued')
            resumed = project_resume('asd', paths=paths)
            self.assertEqual(resumed['status'], 'resumed')
            self.assertEqual(resumed['next_action'], 'Read auth-cookies.md')
            done = project_mark_pending('asd', status='done', note='completed', paths=paths)
            self.assertEqual(done['status'], 'marked')
            status = project_status('asd', paths=paths)
            self.assertEqual(status['pending'], [])

    def test_project_memory_writes_to_workspace_when_attached(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / '.harness' / 'memory').mkdir(parents=True)
            (root / '.opencode' / 'skills').mkdir(parents=True)
            paths = HarnessPaths(root=root, global_root=root / '.global')
            project_create('asd', paths=paths)
            save_project_fact('The login flow uses test OAuth.', paths=paths)
            save_finding('Auth flow mapped', 'Observed in tests.', paths=paths)
            facts = root / 'workspace' / 'projects' / 'asd' / 'memory' / 'facts.jsonl'
            findings = root / 'workspace' / 'projects' / 'asd' / 'memory' / 'findings.jsonl'
            self.assertIn('test OAuth', facts.read_text())
            self.assertIn('Auth flow mapped', findings.read_text())
            ctx = recall_context('OAuth', include_global=False, limit=5, paths=paths)
            self.assertEqual(ctx['attached_project'], 'asd')
            self.assertTrue(ctx['project_hits'])
            idx = index_project(include_qdrant=False, paths=paths)
            self.assertEqual(idx['project_id'], 'asd')

    def test_new_session_does_not_auto_attach(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            state = project_workspace.session_state_path(root)
            self.assertTrue(state.exists())
            state.write_text('{"session_id":"old","project_id":"asd","status":"active"}', encoding='utf-8')
            self.assertIsNone(project_workspace.current_project_id(root))

    def test_registered_repository_registry_stays_fail_closed_when_emptied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            pp = project_workspace.paths_for(root, 'asd')
            child = pp.project_dir / 'repo' / 'one'
            child.mkdir(parents=True)
            added = project_workspace.project_repo_add(root, 'asd', 'one', 'repo/one', default=True)
            self.assertEqual(added['status'], 'registered')
            resolved = project_workspace.resolve_project_repository(root, 'asd', 'one')
            self.assertEqual(resolved['status'], 'ok')
            removed = project_workspace.project_repo_remove(root, 'asd', 'one')
            self.assertEqual(removed['status'], 'removed')
            registry = project_workspace.project_repository_registry(root, 'asd')
            self.assertEqual(registry['mode'], 'registered')
            self.assertEqual(registry['items'], {})
            self.assertEqual(project_workspace.resolve_project_repository(root, 'asd')['status'], 'not_found')

    def test_legacy_default_repository_can_be_selected_explicitly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            resolved = project_workspace.resolve_project_repository(root, 'asd', 'default')
            self.assertEqual(resolved['status'], 'ok')
            self.assertTrue(resolved['legacy'])
            self.assertEqual(resolved['path'], 'repo')

    def test_non_git_source_manifest_is_deterministic_and_changes_with_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            pp = project_workspace.paths_for(root, 'asd')
            source = pp.sources_dir / 'smali'
            source.mkdir(parents=True)
            (source / 'b.smali').write_text('.class public LB;\n', encoding='utf-8')
            (source / 'a.smali').write_text('.class public LA;\n', encoding='utf-8')
            first = project_workspace.source_manifest_identity(source)

            copy = pp.sources_dir / 'copy'
            copy.mkdir()
            (copy / 'a.smali').write_text('.class public LA;\n', encoding='utf-8')
            (copy / 'b.smali').write_text('.class public LB;\n', encoding='utf-8')
            second = project_workspace.source_manifest_identity(copy)
            self.assertEqual(first['content_identity'], second['content_identity'])

            (copy / 'b.smali').write_text('.class public LB;\n# changed\n', encoding='utf-8')
            changed = project_workspace.source_manifest_identity(copy)
            self.assertNotEqual(first['content_identity'], changed['content_identity'])

    def test_non_git_source_registry_is_fail_closed_and_git_ids_cannot_be_shadowed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_workspace.project_create(root, 'asd')
            pp = project_workspace.paths_for(root, 'asd')
            source = pp.sources_dir / 'smali'
            source.mkdir(parents=True)
            (source / 'Auth.smali').write_text('.class public LAuth;\n', encoding='utf-8')
            added = project_workspace.project_source_add(
                root, 'asd', 'smali', 'sources/smali', source_type='smali', default=True
            )
            self.assertEqual(added['status'], 'registered')
            resolved = project_workspace.resolve_project_source(root, 'asd', 'smali')
            self.assertEqual(resolved['status'], 'ok')
            self.assertEqual(resolved['source_type'], 'smali')

            outside = pp.project_dir / 'outside'
            outside.mkdir()
            with self.assertRaises(ValueError):
                project_workspace.project_source_add(root, 'asd', 'outside', 'outside')

            git_child = pp.project_dir / 'repo' / 'app'
            git_child.mkdir(parents=True)
            project_workspace.project_repo_add(root, 'asd', 'app', 'repo/app', default=True)
            with self.assertRaises(ValueError):
                project_workspace.project_source_add(root, 'asd', 'app', 'sources/smali')


if __name__ == '__main__':
    unittest.main()
