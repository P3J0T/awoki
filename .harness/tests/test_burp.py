from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import burp
import project_workspace
from harness_core import HarnessPaths, global_records, index_global, search_rag, project_resume


class BurpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ['AWOKI_GLOBAL_ROOT'] = str(self.root / 'global')
        os.environ['AWOKI_ROOT'] = str(self.root / 'project')
        os.environ['AWOKI_PROJECT_ID'] = 'burp-test'
        os.environ['AWOKI_EMBEDDING_PROVIDER'] = 'hash'
        os.environ['AWOKI_ALLOW_HASH_EMBEDDINGS'] = '1'
        (self.root / 'project' / '.harness' / 'memory').mkdir(parents=True)
        (self.root / 'project' / '.harness' / 'artifacts').mkdir(parents=True)
        (self.root / 'project' / '.harness' / 'index').mkdir(parents=True)
        (self.root / 'project' / '.opencode' / 'skills').mkdir(parents=True)
        (self.root / 'project' / '.harness' / 'manifest.json').write_text('{"harness_version":"test","active_project_id":"burp-test"}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()
        for key in ['AWOKI_GLOBAL_ROOT','AWOKI_ROOT','AWOKI_PROJECT_ID','AWOKI_EMBEDDING_PROVIDER','AWOKI_ALLOW_HASH_EMBEDDINGS']:
            os.environ.pop(key, None)

    def test_burp_redaction_preserves_auth_endpoints_and_parameter_names(self):
        raw = (
            "GET /auth/token?token=super-secret-value&redirect=/auth/callback HTTP/1.1\r\n"
            "Host: example.test\r\n"
            "Authorization: Bearer eyJabcdefghijk.abcdefghijk.abcdefghijk\r\n\r\n"
        )
        safe = burp.redact(raw)
        self.assertIn("/auth/token", safe)
        self.assertIn("token=<VALUE_REDACTED>", safe)
        self.assertIn("redirect=/auth/callback", safe)
        self.assertIn("Authorization: <AUTH_REDACTED>", safe)
        self.assertNotIn("super-secret-value", safe)

    def test_generic_project_has_no_burp_artifacts_until_burp_write(self):
        root = self.root / 'project'
        pp = project_workspace.ensure_project_layout(root, 'plain-project')
        self.assertFalse((pp.artifacts_dir / 'burp').exists())
        project_workspace.refresh_project_files(root, 'plain-project')
        self.assertNotIn('Burp', pp.handoff.read_text(encoding='utf-8'))
        burp.burp_record_observation(project='plain-project', title='Observed login', summary='POST /login')
        self.assertTrue((pp.artifacts_dir / 'burp' / 'observations.jsonl').exists())

    def test_default_profile_uses_docker_host(self):
        st = burp.ensure_store()
        profile = json.loads(Path(st['profile']).read_text(encoding='utf-8'))
        self.assertEqual(profile['targets']['local']['base_url'], 'http://host.docker.internal:9876')
        self.assertIn('repeater', profile['sources'])
        self.assertEqual(profile['sources']['repeater']['tool_alias'], 'active_editor')

    def test_save_result_keeps_raw_and_indexes_only_redacted_summaries(self):
        run = burp.create_run('history', target='local', project_related='demo', tags=['auth'])
        result = {
            'result': {
                'content': [{
                    'type': 'text',
                    'text': json.dumps({
                        'id': 7,
                        'request': 'GET /api/users/123?token=SECRET HTTP/1.1\r\nHost: api.example.test\r\nAuthorization: Bearer REALTOKEN\r\nCookie: sessionid=COOKIEVALUE; theme=dark\r\n\r\n',
                        'response': 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nSet-Cookie: csrf=TOKENVALUE\r\n\r\n{}'
                    })
                }]
            }
        }
        rows = burp.save_result(run, 'sample', result, source_type='history', target='local', project_related='demo', tags=['auth'])
        burp.append_rows(run, rows)
        burp.rebuild(run)
        raw = run / 'raw' / 'sample.mcp.json'
        red = run / 'redacted' / 'sample.txt'
        self.assertTrue(raw.exists())
        self.assertTrue(red.exists())
        self.assertIn('REALTOKEN', raw.read_text(encoding='utf-8'))
        self.assertNotIn('REALTOKEN', red.read_text(encoding='utf-8'))
        endpoints = (run / 'endpoints.md').read_text(encoding='utf-8')
        self.assertIn('/api/users/123', endpoints)
        records = burp.burp_records_for_rag()
        blob = json.dumps(records)
        self.assertIn('api.example.test', blob)
        self.assertNotIn('REALTOKEN', blob)
        self.assertNotIn('COOKIEVALUE', blob)

    def test_global_rag_sees_burp_inventory_not_raw(self):
        run = burp.create_run('history', target='local', project_related='demo', tags=['auth'])
        row = burp.parse_http_pair(
            'POST /login HTTP/1.1\r\nHost: app.example.test\r\nContent-Type: application/json\r\n\r\n{"username":"a","password":"SECRET"}',
            'HTTP/1.1 302 Found\r\nLocation: /home\r\n\r\n',
            run.name,
            'history',
            str(run / 'raw' / 'manual.mcp.json'),
            burp_id=1,
            target='local',
            project_related='demo',
            tags=['auth'],
        )
        burp.append_rows(run, [row])
        burp.rebuild(run)
        paths = HarnessPaths.from_env()
        records = global_records(paths)
        self.assertFalse(any(r.get('kind') == 'burp_inventory' for r in records))
        explicit = global_records(paths, include_burp=True)
        self.assertTrue(any(r.get('kind') == 'burp_inventory' for r in explicit))
        idx = index_global(include_qdrant=False, paths=paths)
        self.assertEqual(idx['status'], 'indexed')
        hits = search_rag('login app.example.test', scope='global', include_global=False, limit=5, paths=paths)
        self.assertFalse(hits['global_hits'])
        self.assertNotIn('SECRET', json.dumps(explicit))


    def test_generic_global_hit_filter_blocks_stale_burp_vector_inventory(self):
        hits = [
            {"kind": "burp_inventory", "source_path": "global:burp/run", "preview": "login"},
            {"kind": "memory", "source_path": "global:memory", "preview": "normal"},
        ]
        import harness_core
        filtered = harness_core._generic_global_hits(hits)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["kind"], "memory")

    def test_project_prefixed_run_and_project_pointer(self):
        project = self.root / 'project' / 'workspace' / 'projects' / 'asd'
        (project / 'artifacts' / 'burp').mkdir(parents=True, exist_ok=True)
        (project / 'project.json').write_text('{"project_id":"asd"}', encoding='utf-8')
        run = burp.create_run('history_regex', target='local', project_related='asd', tags=['auth'])
        self.assertTrue(run.name.startswith('asd__'))
        self.assertTrue(run.name.endswith('__history_regex'))
        row = burp.parse_http_pair(
            'GET /fp/es.js HTTP/1.1\r\nHost: app.example.test\r\n\r\n',
            'HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n\r\n',
            run.name,
            'history_regex',
            str(run / 'raw' / 'manual.mcp.json'),
            burp_id=5,
            target='local',
            project_related='asd',
            tags=['auth'],
        )
        burp.append_rows(run, [row])
        burp.rebuild(run)
        runs_jsonl = project / 'artifacts' / 'burp' / 'runs.jsonl'
        self.assertTrue(runs_jsonl.exists())
        self.assertIn(run.name, runs_jsonl.read_text(encoding='utf-8'))
        self.assertTrue((project / 'artifacts' / 'burp' / 'latest.md').exists())

    def test_project_adapter_surfaces_continuity_refresh_warning(self):
        with mock.patch.object(burp, "refresh_project_handoff_if_possible", return_value="project handoff refresh failed"):
            result = burp.burp_record_observation(
                project="demo",
                title="Observed login behavior",
                summary="The login endpoint returned a session cookie.",
                host="app.example.test",
            )
        self.assertEqual(result["status"], "saved")
        self.assertIn("project handoff refresh failed", result.get("continuity_warning", ""))

    def test_global_run_prefix_when_no_project(self):
        run = burp.create_run('history', target='local')
        self.assertTrue(run.name.startswith('global__'))
        self.assertTrue(run.name.endswith('__history'))

    def test_extract_request_writes_project_local_http_file(self):
        project = self.root / 'project' / 'workspace' / 'projects' / 'asd'
        (project / 'artifacts' / 'burp').mkdir(parents=True, exist_ok=True)
        (project / 'project.json').write_text('{"project_id":"asd"}', encoding='utf-8')
        run = burp.create_run('history', target='local', project_related='asd')
        raw_payload = {
            'result': {'content': [{'type': 'text', 'text': json.dumps({
                'id': 44,
                'request': 'GET /fp/es.js HTTP/1.1\r\nHost: app.example.test\r\nCookie: sid=secret\r\n\r\n',
                'response': 'HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n\r\n'
            })}]}
        }
        rows = burp.save_result(run, 'history-44', raw_payload, source_type='history', target='local', project_related='asd')
        burp.append_rows(run, rows)
        burp.rebuild(run)
        out = burp.extract_request(pattern='fp/es.js', run_dir=str(run), name='fp_es', project_related='asd')
        path = Path(out['output'])
        self.assertTrue(path.exists())
        self.assertIn('workspace/projects/asd/artifacts/burp/extracted', str(path))
        self.assertIn('GET /fp/es.js HTTP/1.1', path.read_text(encoding='utf-8'))

    def test_tool_discovery_aliases_portswigger_names(self):
        tools = [{'name':'get_proxy_http_history'}, {'name':'get_active_editor_contents'}]
        self.assertEqual(burp.discover_tool_name(tools, 'history'), 'get_proxy_http_history')
        self.assertEqual(burp.discover_tool_name(tools, 'repeater'), None)
        self.assertEqual(burp.discover_tool_name(tools, 'active_editor'), 'get_active_editor_contents')

    def test_validate_run(self):
        run = burp.create_run('active_editor')
        burp.rebuild(run)
        result = burp.validate_run(run)
        self.assertEqual(result['status'], 'ok')



class BurpActiveActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ['AWOKI_GLOBAL_ROOT'] = str(self.root / 'global')
        os.environ['AWOKI_ROOT'] = str(self.root / 'project')
        os.environ['AWOKI_PROJECT_ID'] = 'burp-active-test'
        (self.root / 'project' / '.harness' / 'memory').mkdir(parents=True)
        (self.root / 'project' / '.harness' / 'artifacts').mkdir(parents=True)
        (self.root / 'project' / '.harness' / 'index').mkdir(parents=True)
        self.calls = []
        self.original_client_for_target = burp.client_for_target

        class FakeClient:
            def __init__(inner):
                pass
            def tools(inner):
                return {'result': {'tools': [
                    {'name': 'send_http1_request'},
                    {'name': 'create_repeater_tab'},
                    {'name': 'send_to_intruder'},
                    {'name': 'get_active_editor_contents'},
                    {'name': 'set_active_editor_contents'},
                ]}}
            def call_tool(inner, name, arguments, timeout=180):
                self.calls.append((name, arguments))
                if name == 'get_active_editor_contents':
                    return {'result': {'content': [{'type': 'text', 'text': 'GET /active HTTP/1.1\r\nHost: app.example.test\r\n\r\n'}]}}
                if name == 'send_http1_request':
                    return {'result': {'content': [{'type': 'text', 'text': 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nok'}]}}
                return {'result': {'content': [{'type': 'text', 'text': 'ok'}]}}
        burp.client_for_target = lambda target='local': FakeClient()

    def tearDown(self):
        burp.client_for_target = self.original_client_for_target
        self.tmp.cleanup()
        for key in ['AWOKI_GLOBAL_ROOT','AWOKI_ROOT','AWOKI_PROJECT_ID']:
            os.environ.pop(key, None)

    def test_build_raw_request_infers_https_service(self):
        raw, host, port, https = burp.build_raw_http1_request('POST', 'https://api.example.test/v1/users?id=1', [('Content-Type','application/json')], '{"x":1}')
        self.assertIn('POST /v1/users?id=1 HTTP/1.1', raw)
        self.assertIn('Host: api.example.test', raw)
        self.assertIn('Content-Length: 7', raw)
        self.assertEqual((host, port, https), ('api.example.test', 443, True))

    def test_send_request_uses_burp_mcp_and_saves_evidence(self):
        result = burp.send_request('GET', 'https://app.example.test/api', headers=[('Authorization','Bearer SECRET')], target='local')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.calls[0][0], 'send_http1_request')
        self.assertEqual(self.calls[0][1]['targetHostname'], 'app.example.test')
        self.assertEqual(self.calls[0][1]['targetPort'], 443)
        raw = Path(result['evidence_file']).read_text(encoding='utf-8')
        red = Path(result['redacted_file']).read_text(encoding='utf-8')
        self.assertIn('SECRET', raw)
        self.assertNotIn('SECRET', red)

    def test_active_to_intruder_uses_active_editor_then_intruder(self):
        result = burp.active_to_intruder(target='local', tab_name='active-test')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([c[0] for c in self.calls[:2]], ['get_active_editor_contents', 'send_to_intruder'])
        self.assertIn('GET /active HTTP/1.1', self.calls[1][1]['content'])
        self.assertEqual(self.calls[1][1]['tabName'], 'active-test')

    def test_history_to_repeater_recovers_raw_request_from_evidence(self):
        run = burp.create_run('history', target='local')
        raw_payload = {
            'result': {'content': [{'type': 'text', 'text': json.dumps({
                'id': 77,
                'request': 'POST /login HTTP/1.1\r\nHost: app.example.test\r\n\r\nusername=a',
                'response': 'HTTP/1.1 302 Found\r\nLocation: /home\r\n\r\n'
            })}]}
        }
        rows = burp.save_result(run, 'history-77', raw_payload, source_type='history', target='local')
        burp.append_rows(run, rows)
        burp.rebuild(run)
        result = burp.history_to_repeater(77, target='local', run_dir=str(run), tab_name='from-history')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.calls[0][0], 'create_repeater_tab')
        self.assertIn('POST /login HTTP/1.1', self.calls[0][1]['content'])
        self.assertEqual(self.calls[0][1]['tabName'], 'from-history')

class BurpReaderToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ['AWOKI_GLOBAL_ROOT'] = str(self.root / 'global')
        os.environ['AWOKI_ROOT'] = str(self.root / 'project')
        (self.root / 'project' / 'workspace' / 'projects' / 'ASDF-101' / 'artifacts' / 'burp').mkdir(parents=True)
        (self.root / 'project' / 'workspace' / 'projects' / 'ASDF-101' / 'project.json').write_text('{"project_id":"ASDF-101"}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()
        for key in ['AWOKI_GLOBAL_ROOT','AWOKI_ROOT']:
            os.environ.pop(key, None)

    def test_rows_from_truncated_portswigger_mcp_text(self):
        run = burp.create_run('history_regex', target='local', project_related='ASDF-101')
        text = '{"id":77,"request":"GET /fp/es.js HTTP/1.1\\r\\nHost: app.example.test\\r\\nCookie: sid=secret\\r\\n\\r\\n","response":"HTTP/1.1 200 OK\\r\\nContent-Type: application/javascript\\r\\n\\r\\nconsole.log(1)... (truncated)'
        result = {'result': {'content': [{'type': 'text', 'text': text}]}}
        rows = burp.save_result(run, 'truncated', result, source_type='history_regex', target='local', project_related='ASDF-101')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['method'], 'GET')
        self.assertEqual(rows[0]['host'], 'app.example.test')
        self.assertEqual(rows[0]['path'], '/fp/es.js')
        self.assertEqual(rows[0]['status_code'], 200)

    def test_find_request_has_no_fixed_run_limit_by_default(self):
        older = []
        for i in range(4):
            run = burp.create_run('history', target='local', project_related='ASDF-101')
            row = burp.parse_http_pair(
                f'GET /item/{i} HTTP/1.1\r\nHost: app.example.test\r\n\r\n',
                'HTTP/1.1 200 OK\r\n\r\n',
                run.name,
                'history',
                str(run / 'raw' / 'manual.mcp.json'),
                burp_id=i,
                target='local',
                project_related='ASDF-101',
            )
            # Save minimal evidence so include_raw path can recover if needed.
            burp.write_json(run / 'raw' / 'manual.mcp.json', {'request': f'GET /item/{i} HTTP/1.1\r\nHost: app.example.test\r\n\r\n', 'response': 'HTTP/1.1 200 OK\r\n\r\n'})
            burp.append_rows(run, [row])
            burp.rebuild(run)
            older.append(run)
        default_res = burp.burp_find_request('item/0', project_related='ASDF-101')
        self.assertEqual(default_res['status'], 'ok')
        self.assertEqual(default_res['match_count'], 1)
        self.assertIn(':req:', default_res['matches'][0]['request_ref'])
        bounded = burp.burp_find_request('item/0', project_related='ASDF-101', limit_runs=3)
        self.assertEqual(bounded['status'], 'no_matches')


    def test_host_report_scans_relevant_runs_and_writes_project_artifacts(self):
        project = self.root / 'project' / 'workspace' / 'projects' / 'ASDF-101'
        (project / 'artifacts' / 'burp').mkdir(parents=True, exist_ok=True)
        (project / 'project.json').write_text('{"project_id":"ASDF-101"}', encoding='utf-8')
        for i, path in enumerate(['/login', '/static/app.js', '/api/account/123']):
            run = burp.create_run('history', target='local', project_related='ASDF-101')
            row = burp.parse_http_pair(
                f'GET {path}?token=SECRET HTTP/1.1\r\nHost: app.example.test\r\nCookie: sid=secret; theme=light\r\n\r\n',
                'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nSet-Cookie: csrf=secret\r\n\r\n',
                run.name,
                'history',
                str(run / 'raw' / f'manual-{i}.mcp.json'),
                burp_id=i,
                target='local',
                project_related='ASDF-101',
            )
            burp.append_rows(run, [row])
            burp.rebuild(run)
        report = burp.burp_host_report('app.example.test', project_related='ASDF-101')
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['requests_found'], 3)
        self.assertEqual(report['runs_considered'], 3)
        self.assertIn('artifact_paths', report)
        md = Path(report['artifact_paths']['markdown'])
        self.assertTrue(md.exists())
        body = md.read_text(encoding='utf-8')
        self.assertIn('/api/account/123', body)
        self.assertNotIn('secret;', body.lower())

    def test_show_and_extract_by_request_ref(self):
        run = burp.create_run('history', target='local', project_related='ASDF-101')
        raw_payload = {
            'result': {'content': [{'type': 'text', 'text': json.dumps({
                'id': 44,
                'request': 'GET /fp/es.js HTTP/1.1\r\nHost: app.example.test\r\nCookie: sid=secret\r\n\r\n',
                'response': 'HTTP/1.1 200 OK\r\nContent-Type: application/javascript\r\n\r\n'
            })}]}
        }
        rows = burp.save_result(run, 'history-44', raw_payload, source_type='history', target='local', project_related='ASDF-101')
        burp.append_rows(run, rows)
        burp.rebuild(run)
        ref = burp.request_ref(rows[0])
        shown = burp.burp_show_request(request_ref_value=ref)
        self.assertIn('GET /fp/es.js HTTP/1.1', shown['request_preview_redacted'])
        self.assertIn('<COOKIE_REDACTED>', shown['request_preview_redacted'])
        out = burp.extract_request(request_ref_value=ref, name='fp_es', project_related='ASDF-101')
        self.assertTrue(Path(out['output']).exists())
        self.assertIn('/fp/es.js', Path(out['output']).read_text(encoding='utf-8'))

class BurpHybridEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ['AWOKI_GLOBAL_ROOT'] = str(self.root / 'global')
        os.environ['AWOKI_ROOT'] = str(self.root / 'project')
        project = self.root / 'project' / 'workspace' / 'projects' / 'ASDF-101'
        (project / 'artifacts' / 'burp').mkdir(parents=True)
        (project / 'memory').mkdir(parents=True)
        (project / 'notes').mkdir(parents=True)
        (project / 'project.json').write_text('{"project_id":"ASDF-101","status":"active"}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()
        for key in ['AWOKI_GLOBAL_ROOT','AWOKI_ROOT']:
            os.environ.pop(key, None)

    def test_record_observation_writes_project_artifact_and_finding(self):
        res = burp.burp_record_observation(
            project='ASDF-101',
            title='Login endpoint observed',
            summary='POST /login returns a session cookie and redirects to /home.',
            host='app.example.test',
            method='POST',
            path='/login',
            status_code='302',
            next_action='Open login request in Repeater.'
        )
        self.assertEqual(res['status'], 'saved')
        obs = self.root / 'project' / 'workspace' / 'projects' / 'ASDF-101' / 'artifacts' / 'burp' / 'observations.jsonl'
        self.assertTrue(obs.exists())
        body = obs.read_text(encoding='utf-8')
        self.assertIn('Login endpoint observed', body)
        findings = self.root / 'project' / 'workspace' / 'projects' / 'ASDF-101' / 'memory' / 'findings.jsonl'
        self.assertTrue(findings.exists())
        self.assertIn('session cookie', findings.read_text(encoding='utf-8'))

    def test_save_host_summary_and_task_checkpoint(self):
        res = burp.burp_save_host_summary(
            project='ASDF-101',
            hostname='https://app.example.test/path',
            summary='Live Burp MCP review found 18 raw matches and 7 endpoints.',
            coverage={'live_burp_checked': True, 'raw_matches': 18, 'unique_endpoints': 7},
            request_refs=['burp-live:req:1'],
            next_action='Send account API to Repeater.'
        )
        self.assertEqual(res['status'], 'saved')
        self.assertTrue(Path(res['markdown_path']).exists())
        self.assertIn('app.example.test', Path(res['markdown_path']).read_text(encoding='utf-8'))
        ck = burp.burp_task_checkpoint(
            project='ASDF-101',
            title='Review app.example.test auth flow',
            current_step='Summarized host traffic.',
            completed_steps=['Checked live proxy history'],
            remaining_steps=['Open account API in Repeater'],
            next_action='Send account API request to Repeater.',
            related_refs=['burp-live:req:1'],
        )
        self.assertEqual(ck['status'], 'checkpointed')
        st = burp.burp_task_status(project='ASDF-101')
        self.assertEqual(st['status'], 'ok')
        self.assertIn('Send account API', st['next_action'])
        fin = burp.burp_task_finalize(project='ASDF-101', outcome='Done', finding='Account API uses session cookie.', next_action='Test IDOR candidates.')
        self.assertEqual(fin['status'], 'finalized')
        paths = HarnessPaths(root=self.root / 'project', global_root=self.root / 'global')
        resumed = project_resume('ASDF-101', paths=paths)
        self.assertIn('Burp host summary', resumed['handoff'])
        self.assertIn('Account API uses session cookie', resumed['handoff'])

    def test_adapter_failure_is_reported_without_losing_primary_burp_result(self):
        with mock.patch.object(project_workspace, 'project_capture', side_effect=RuntimeError('continuity unavailable')):
            result = burp.burp_record_observation(
                project='ASDF-101',
                title='Saved despite adapter warning',
                summary='Compact observation.',
            )
        self.assertEqual(result['status'], 'saved')
        self.assertIn('continuity_warning', result)
        self.assertIn('continuity unavailable', result['continuity_warning'])


if __name__ == '__main__':
    unittest.main()
