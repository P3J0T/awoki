from __future__ import annotations

import argparse
import calendar
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import project_workspace
import runtime_safety
from code_search import engine as code_search

STATE_VERSION = 1
DEFAULT_POLL_SECONDS = 10


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _poll_seconds() -> int:
    try:
        value = int(os.environ.get('AWOKI_CODE_INDEX_POLL_SECONDS', str(DEFAULT_POLL_SECONDS)))
    except ValueError:
        value = DEFAULT_POLL_SECONDS
    return max(2, min(value, 120))


def _iso_after(seconds: int) -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + max(0, seconds)))


def _jobs_dir(root: Path, project_id: str) -> Path:
    pp = project_workspace.paths_for(root, project_id)
    return pp.index_dir / 'jobs' / 'code-index'


def _state_path(root: Path, project_id: str, job_id: str) -> Path:
    return _jobs_dir(root, project_id) / f'{job_id}.json'


def _log_path(root: Path, project_id: str, job_id: str) -> Path:
    return _jobs_dir(root, project_id) / f'{job_id}.log'


@contextmanager
def _lock(root: Path, project_id: str) -> Iterator[None]:
    directory = _jobs_dir(root, project_id)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / '.lock'
    with lock_path.open('a+', encoding='utf-8') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write('\n')
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _registered_repo_ids(root: Path, project_id: str) -> list[str]:
    return [
        str(row.get('repo_id') or '')
        for row in project_workspace.project_repositories(root, project_id)
        if str(row.get('repo_id') or '')
    ]


def _registered_source_ids(root: Path, project_id: str) -> list[str]:
    return [
        str(row.get('source_id') or '')
        for row in project_workspace.project_sources(root, project_id)
        if str(row.get('source_id') or '') and str(row.get('source_type') or '') != 'git'
    ]


def _normalize_scope(root: Path, project_id: str, repo: str, source_id: str = '') -> tuple[str, list[str], dict[str, Any] | None]:
    if repo and source_id:
        return '', [], {
            'status': 'rejected', 'project_id': project_id, 'repo': repo, 'source_id': source_id,
            'reason': 'repo and source_id are mutually exclusive selectors',
        }
    repos = _registered_repo_ids(root, project_id)
    if source_id:
        sources = _registered_source_ids(root, project_id)
        if source_id not in sources:
            return '', [], {
                'status': 'not_found', 'project_id': project_id, 'source_id': source_id,
                'reason': 'evidence source is not registered/enabled',
            }
        return 'source', [source_id], None
    if repo:
        if repo not in repos:
            return '', [], {
                'status': 'not_found', 'project_id': project_id, 'repo': repo,
                'reason': 'repository is not registered/enabled',
            }
        return 'repository', [repo], None
    if not repos:
        return '', [], {'status': 'not_found', 'project_id': project_id, 'reason': 'project has no enabled repositories'}
    return 'repository', repos, None


def _scope_key(scope_type: str, scope_ids: list[str]) -> str:
    return f'{scope_type}:' + ','.join(sorted(scope_ids))


def _elapsed_seconds(started_at: str, finished_at: str = '') -> float:
    if not started_at:
        return 0.0
    try:
        started = calendar.timegm(time.strptime(started_at, '%Y-%m-%dT%H:%M:%SZ'))
        ended = calendar.timegm(time.strptime(finished_at, '%Y-%m-%dT%H:%M:%SZ')) if finished_at else time.time()
        return round(max(0.0, ended - started), 1)
    except Exception:
        return 0.0


def _job_progress_summary(state: dict[str, Any]) -> dict[str, Any]:
    scope_progress = state.get('scope_progress')
    if not isinstance(scope_progress, dict):
        scope_progress = {}
    rows = [row for row in scope_progress.values() if isinstance(row, dict)]

    def total(field: str) -> int:
        return sum(int(row.get(field) or 0) for row in rows)

    files_total = total('files_total')
    files_processed = total('files_processed')
    status = str(state.get('status') or '')
    progress_percent = 100.0 if status == 'completed' else (
        round((files_processed / files_total) * 100.0, 1) if files_total else 0.0
    )
    current = state.get('progress') if isinstance(state.get('progress'), dict) else {}
    scope_type = str(state.get('scope_type') or 'repository')
    current_id = str(state.get('current_source') or state.get('current_repository') or current.get('source_id') or current.get('repo_id') or '')
    phase = str(current.get('phase') or status or 'queued')
    if status in {'completed', 'failed', 'cancelled', 'interrupted'}:
        phase = status
    parse_modes: dict[str, int] = {}
    for row in rows:
        for key, value in (row.get('parse_modes') or {}).items():
            parse_modes[str(key)] = parse_modes.get(str(key), 0) + int(value or 0)
    return {
        'phase': phase,
        'scope_type': scope_type,
        'current_repository': current_id if scope_type == 'repository' else '',
        'current_source': current_id if scope_type == 'source' else '',
        'current_path': str(current.get('current_path') or ''),
        'files_total': files_total,
        'files_processed': files_processed,
        'files_parsed': total('files_parsed'),
        'files_reused': total('files_reused'),
        'files_removed': total('files_removed'),
        'parse_modes': parse_modes,
        'progress_percent': progress_percent,
        'elapsed_seconds': _elapsed_seconds(str(state.get('started_at') or ''), str(state.get('finished_at') or '')),
        'last_progress_at': str(current.get('updated_at') or state.get('updated_at') or ''),
        'repositories': scope_progress if scope_type == 'repository' else {},
        'sources': scope_progress if scope_type == 'source' else {},
        'reason': str(current.get('reason') or state.get('reason') or ''),
    }


def _update_progress(state_path: Path, scope_id: str, payload: dict[str, Any]) -> None:
    state = _read_json(state_path)
    if not state or str(state.get('status') or '') == 'cancelled':
        return
    now = _now()
    scope_progress = state.get('scope_progress')
    if not isinstance(scope_progress, dict):
        scope_progress = {}
    scope_type = str(state.get('scope_type') or 'repository')
    identity_key = 'source_id' if scope_type == 'source' else 'repo_id'
    prior = scope_progress.get(scope_id)
    if not isinstance(prior, dict):
        prior = {identity_key: scope_id}
    current = {**prior, **payload, identity_key: scope_id, 'updated_at': now}
    scope_progress[scope_id] = current
    state['scope_progress'] = scope_progress
    state['progress'] = current
    state['updated_at'] = now
    _write_json(state_path, state)


def _latest_states(root: Path, project_id: str) -> list[dict[str, Any]]:
    directory = _jobs_dir(root, project_id)
    if not directory.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in directory.glob('*.json'):
        row = _read_json(path)
        if row:
            rows.append(row)
    rows.sort(key=lambda row: str(row.get('created_at') or ''), reverse=True)
    return rows


def _refresh_stale_running_state(root: Path, project_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if str(state.get('status') or '') not in {'queued', 'running'}:
        return state
    pid = int(state.get('pid') or 0)
    if _pid_alive(pid):
        return state
    state = dict(state)
    state['status'] = 'interrupted'
    state['updated_at'] = _now()
    state['finished_at'] = state['updated_at']
    state['reason'] = 'worker process is no longer running before a terminal result was recorded'
    _write_json(_state_path(root, project_id, str(state.get('job_id') or '')), state)
    return state


def active(root: Path, project_id: str) -> list[dict[str, Any]]:
    with _lock(root, project_id):
        rows: list[dict[str, Any]] = []
        for state in _latest_states(root, project_id):
            state = _refresh_stale_running_state(root, project_id, state)
            if str(state.get('status') or '') in {'queued', 'running'}:
                rows.append(state)
        return rows


def start(root: Path, project_id: str, *, repo: str = '', source_id: str = '', force: bool = False) -> dict[str, Any]:
    pp = project_workspace.paths_for(root, project_id)
    if not pp.project_json.exists():
        return {'status': 'not_found', 'project_id': project_id}
    scope_type, scope_ids, error = _normalize_scope(root, project_id, repo, source_id)
    if error:
        return error
    scope_key = _scope_key(scope_type, scope_ids)
    with _lock(root, project_id):
        for state in _latest_states(root, project_id):
            state = _refresh_stale_running_state(root, project_id, state)
            if str(state.get('status') or '') in {'queued', 'running'} and str(state.get('scope_key') or '') == scope_key:
                return {
                    'status': 'already_running',
                    'project_id': project_id,
                    'job': state,
                    'message': 'A matching local code-index refresh is already running. Do not start another or autonomously poll; check code_index_refresh_status when the user asks or when a later requested action needs fresh state.',
                }
        job_id = 'cir_' + uuid.uuid4().hex[:16]
        now = _now()
        identity_key = 'source_id' if scope_type == 'source' else 'repo_id'
        state = {
            'schema_version': STATE_VERSION,
            'job_id': job_id,
            'project_id': project_id,
            'scope_type': scope_type,
            'scope_ids': scope_ids,
            'scope_key': scope_key,
            'force': bool(force),
            'status': 'queued',
            'pid': 0,
            'created_at': now,
            'started_at': '',
            'updated_at': now,
            'finished_at': '',
            'completed_scopes': 0,
            'total_scopes': len(scope_ids),
            'current_repository': '',
            'current_source': '',
            'progress': {'phase': 'queued', identity_key: '', 'updated_at': now},
            'scope_progress': {
                scope_id: {
                    identity_key: scope_id,
                    'phase': 'queued',
                    'files_total': 0,
                    'files_processed': 0,
                    'files_parsed': 0,
                    'files_reused': 0,
                    'files_removed': 0,
                    'current_path': '',
                    'parse_modes': {},
                    'progress_percent': 0.0,
                    'updated_at': now,
                }
                for scope_id in scope_ids
            },
            'results': [],
            'log_path': str(_log_path(root, project_id, job_id).relative_to(root)),
        }
        _write_json(_state_path(root, project_id, job_id), state)
        log_path = _log_path(root, project_id, job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open('ab', buffering=0) as log_handle:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), '--worker', '--root', str(root), '--project', project_id, '--job-id', job_id],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                env=runtime_safety.credential_free_environment(),
            )
        state['pid'] = int(proc.pid)
        state['status'] = 'running'
        state['started_at'] = _now()
        state['updated_at'] = state['started_at']
        _write_json(_state_path(root, project_id, job_id), state)
    return {
        'status': 'started',
        'project_id': project_id,
        'job': state,
        'progress': _job_progress_summary(state),
        'message': 'Local structural/FTS code indexing is running in a detached worker. MCP remains responsive. Do not autonomously poll; check code_index_refresh_status when the user asks or when a later requested action needs fresh state.',
        'network': False,
        'remote_embedding': False,
        'recommended_poll_after_seconds': _poll_seconds(),
        'next_poll_after': _iso_after(_poll_seconds()),
    }


def status(root: Path, project_id: str, *, job_id: str = '', repo: str = '', source_id: str = '') -> dict[str, Any]:
    pp = project_workspace.paths_for(root, project_id)
    if not pp.project_json.exists():
        return {'status': 'not_found', 'project_id': project_id}
    with _lock(root, project_id):
        if job_id:
            state = _read_json(_state_path(root, project_id, job_id))
            if not state:
                return {'status': 'not_found', 'project_id': project_id, 'job_id': job_id}
            state = _refresh_stale_running_state(root, project_id, state)
        else:
            scope_type, scope_ids, error = _normalize_scope(root, project_id, repo, source_id)
            if error:
                return error
            candidates = [
                row for row in _latest_states(root, project_id)
                if str(row.get('scope_type') or '') == scope_type
                and sorted(str(value) for value in (row.get('scope_ids') or [])) == sorted(scope_ids)
            ]
            if not candidates:
                return {'status': 'not_found', 'project_id': project_id, 'reason': 'no matching local code-index refresh job exists'}
            state = _refresh_stale_running_state(root, project_id, candidates[0])
        cooldown = _poll_seconds()
        now_epoch = time.time()
        terminal = str(state.get('status') or '') in {'completed', 'failed', 'cancelled', 'interrupted'}
        last_accepted = float(state.get('last_status_request_epoch') or 0.0)
        retry_after = 0
        poll_too_soon = False
        if not terminal and last_accepted > 0:
            retry_after = max(0, int(round(cooldown - (now_epoch - last_accepted))))
            poll_too_soon = retry_after > 0
        if not terminal and not poll_too_soon:
            state = dict(state)
            state['last_status_request_epoch'] = now_epoch
            state['last_status_request_at'] = _now()
            _write_json(_state_path(root, project_id, str(state.get('job_id') or '')), state)
    return {
        'status': 'ok',
        'project_id': project_id,
        'job': state,
        'progress': _job_progress_summary(state),
        'recommended_poll_after_seconds': cooldown,
        'poll_too_soon': poll_too_soon,
        'retry_after_seconds': retry_after,
        'next_poll_after': _iso_after(retry_after if poll_too_soon else cooldown) if not terminal else '',
    }


def cancel(root: Path, project_id: str, *, job_id: str = '') -> dict[str, Any]:
    if not job_id:
        return {'status': 'rejected', 'project_id': project_id, 'reason': 'job_id is required for cancellation'}
    with _lock(root, project_id):
        state = _read_json(_state_path(root, project_id, job_id))
        if not state:
            return {'status': 'not_found', 'project_id': project_id, 'job_id': job_id}
        state = _refresh_stale_running_state(root, project_id, state)
        if str(state.get('status') or '') not in {'queued', 'running'}:
            return {'status': 'not_running', 'project_id': project_id, 'job': state}
        pid = int(state.get('pid') or 0)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        state['status'] = 'cancelled'
        state['updated_at'] = _now()
        state['finished_at'] = state['updated_at']
        state['reason'] = 'cancelled by user request'
        _write_json(_state_path(root, project_id, job_id), state)
    return {'status': 'cancelled', 'project_id': project_id, 'job': state}


def _worker(root: Path, project_id: str, job_id: str) -> int:
    state_path = _state_path(root, project_id, job_id)
    state = _read_json(state_path)
    if not state:
        return 2
    paths = SimpleNamespace(root=root)
    results: list[dict[str, Any]] = []
    try:
        scope_type = str(state.get('scope_type') or 'repository')
        scope_ids = [str(value) for value in (state.get('scope_ids') or [])]
        force = bool(state.get('force'))
        identity_key = 'source_id' if scope_type == 'source' else 'repo_id'
        for index, scope_id in enumerate(scope_ids):
            state = _read_json(state_path) or state
            if str(state.get('status') or '') == 'cancelled':
                return 0
            state['status'] = 'running'
            state['current_repository'] = scope_id if scope_type == 'repository' else ''
            state['current_source'] = scope_id if scope_type == 'source' else ''
            state['completed_scopes'] = index
            state['updated_at'] = _now()
            _write_json(state_path, state)
            _update_progress(state_path, scope_id, {'phase': 'preparing'})
            print(f'[code-index-refresh] project={project_id} {scope_type}={scope_id} starting force={force}', flush=True)

            def report_progress(payload: dict[str, Any]) -> None:
                _update_progress(state_path, scope_id, payload)

            result = code_search.index_project_code(
                paths,
                project_id,
                include_qdrant=False,
                force=force,
                repo=scope_id if scope_type == 'repository' else '',
                source=scope_id if scope_type == 'source' else '',
                progress_callback=report_progress,
            )
            freshness = code_search.index_status(
                paths,
                project_id,
                deep_verify=True,
                verify_qdrant=False,
                repo=scope_id if scope_type == 'repository' else '',
                source=scope_id if scope_type == 'source' else '',
            )
            result_row = {identity_key: scope_id, **result, 'freshness_after': freshness.get('freshness') or {}, 'status_after': freshness.get('status')}
            results.append(result_row)
            state = _read_json(state_path) or state
            state['results'] = results
            state['completed_scopes'] = index + 1
            state['current_repository'] = ''
            state['current_source'] = ''
            state['updated_at'] = _now()
            _write_json(state_path, state)
            ok = result.get('status') in {'indexed', 'current'} and bool((freshness.get('freshness') or {}).get('lexical_current'))
            progress = ((state.get('scope_progress') or {}).get(scope_id) or {})
            terminal_progress = {
                **progress,
                'phase': 'scope_complete' if ok else 'failed',
                'progress_percent': 100.0 if ok else float(progress.get('progress_percent') or 0.0),
            }
            if not ok:
                terminal_progress['reason'] = str(result.get('reason') or freshness.get('reason') or 'local code-index refresh failed')
            _update_progress(state_path, scope_id, terminal_progress)
            print(f"[code-index-refresh] project={project_id} {scope_type}={scope_id} status={result.get('status')} lexical_current={bool((freshness.get('freshness') or {}).get('lexical_current'))}", flush=True)

        failures = [
            row for row in results
            if row.get('status') not in {'indexed', 'current'} or not bool((row.get('freshness_after') or {}).get('lexical_current'))
        ]
        state = _read_json(state_path) or state
        state['status'] = 'failed' if failures else 'completed'
        state['reason'] = 'one or more local code-index refreshes failed' if failures else ''
        state['results'] = results
        state['completed_scopes'] = len(scope_ids)
        state['current_repository'] = ''
        state['current_source'] = ''
        state['finished_at'] = _now()
        state['updated_at'] = state['finished_at']
        state['progress'] = {
            **(state.get('progress') if isinstance(state.get('progress'), dict) else {}),
            'phase': state['status'],
            'updated_at': state['finished_at'],
        }
        _write_json(state_path, state)
        return 1 if failures else 0
    except BaseException as exc:
        state = _read_json(state_path) or state
        if str(state.get('status') or '') != 'cancelled':
            state['status'] = 'failed'
            state['reason'] = f'{type(exc).__name__}: {exc}'[:1000]
            state['results'] = results
            state['current_repository'] = ''
            state['current_source'] = ''
            state['finished_at'] = _now()
            state['updated_at'] = state['finished_at']
            state['progress'] = {
                **(state.get('progress') if isinstance(state.get('progress'), dict) else {}),
                'phase': 'failed',
                'reason': state['reason'],
                'updated_at': state['finished_at'],
            }
            _write_json(state_path, state)
        print(f'[code-index-refresh] failed: {type(exc).__name__}: {exc}', flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--root', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--job-id', required=True)
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error('--worker is required')
    return _worker(Path(args.root).resolve(), project_workspace.clean_project_id(args.project), args.job_id)


if __name__ == '__main__':
    raise SystemExit(main())
