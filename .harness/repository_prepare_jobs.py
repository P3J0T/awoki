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

import code_index_jobs
import code_vector_jobs
import project_workspace
import rag_backend
import runtime_safety
from code_search import engine as code_search

STATE_VERSION = 1
DEFAULT_POLL_SECONDS = 15
TERMINAL = {"completed", "blocked", "failed", "cancelled", "interrupted"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_after(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + max(0, seconds)))


def _poll_seconds() -> int:
    try:
        value = int(os.environ.get("AWOKI_REPOSITORY_PREPARE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS)))
    except ValueError:
        value = DEFAULT_POLL_SECONDS
    return max(5, min(value, 120))


def _jobs_dir(root: Path, project_id: str) -> Path:
    return project_workspace.paths_for(root, project_id).index_dir / "jobs" / "repository-prepare"


def _state_path(root: Path, project_id: str, job_id: str) -> Path:
    return _jobs_dir(root, project_id) / f"{job_id}.json"


def _log_path(root: Path, project_id: str, job_id: str) -> Path:
    return _jobs_dir(root, project_id) / f"{job_id}.log"


@contextmanager
def _lock(root: Path, project_id: str) -> Iterator[None]:
    directory = _jobs_dir(root, project_id)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
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


def _elapsed_seconds(state: dict[str, Any]) -> float:
    started_at = str(state.get("started_at") or "")
    finished_at = str(state.get("finished_at") or "")
    if not started_at:
        return 0.0
    try:
        started = calendar.timegm(time.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ"))
        ended = calendar.timegm(time.strptime(finished_at, "%Y-%m-%dT%H:%M:%SZ")) if finished_at else time.time()
        return round(max(0.0, ended - started), 1)
    except Exception:
        return 0.0


def _resolve_scope(root: Path, project_id: str, repo: str, source_id: str) -> tuple[str, str, dict[str, Any] | None]:
    if repo and source_id:
        return "", "", {"status": "rejected", "reason": "repo and source_id are mutually exclusive"}
    repositories = project_workspace.project_repositories(root, project_id)
    sources = project_workspace.project_sources(root, project_id)
    if repo:
        if repo not in {str(row.get("repo_id") or "") for row in repositories}:
            return "", "", {"status": "not_found", "project_id": project_id, "repo": repo}
        return "repository", repo, None
    if source_id:
        if source_id not in {str(row.get("source_id") or "") for row in sources}:
            return "", "", {"status": "not_found", "project_id": project_id, "source_id": source_id}
        source_row = next((row for row in sources if str(row.get("source_id") or "") == source_id), {})
        if str(source_row.get("source_type") or "git") == "git":
            return "repository", str(source_row.get("repo_id") or source_id), None
        return "source", source_id, None
    if len(repositories) == 1:
        return "repository", str(repositories[0].get("repo_id") or ""), None
    non_git = [row for row in sources if str(row.get("source_type") or "git") != "git"]
    if not repositories and len(non_git) == 1:
        return "source", str(non_git[0].get("source_id") or ""), None
    return "", "", {
        "status": "rejected",
        "project_id": project_id,
        "reason": "repository readiness requires one exact managed repo/source; specify repo= or source_id=",
    }


def _scope_key(scope_type: str, scope_id: str, mode: str) -> str:
    return f"{scope_type}:{scope_id}|mode:{mode}"


def _configuration_blockers(mode: str) -> list[str]:
    if mode != "full":
        return []
    snapshot = rag_backend.retrieval_status_snapshot()
    embedding = snapshot.get("embedding") if isinstance(snapshot.get("embedding"), dict) else {}
    rerank = snapshot.get("rerank") if isinstance(snapshot.get("rerank"), dict) else {}
    blockers: list[str] = []
    if not bool(embedding.get("configuration_ready")):
        blockers.append("embedding_configuration_not_ready")
    if not bool(rerank.get("enabled")):
        blockers.append("reranker_disabled")
    elif not bool(rerank.get("configuration_ready")):
        blockers.append("reranker_configuration_not_ready")
    return blockers


def _latest_states(root: Path, project_id: str) -> list[dict[str, Any]]:
    directory = _jobs_dir(root, project_id)
    if not directory.exists():
        return []
    rows = [_read_json(path) for path in directory.glob("*.json")]
    rows = [row for row in rows if row]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def _refresh_stale_running_state(root: Path, project_id: str, state: dict[str, Any]) -> dict[str, Any]:
    if str(state.get("status") or "") not in {"queued", "running"}:
        return state
    if _pid_alive(int(state.get("pid") or 0)):
        return state
    updated = dict(state)
    updated["status"] = "interrupted"
    updated["outcome"] = "PRECONDITION_FAILED"
    updated["reason"] = "repository preparation worker is no longer running before a terminal result was recorded"
    updated["finished_at"] = _now()
    updated["updated_at"] = updated["finished_at"]
    _write_json(_state_path(root, project_id, str(updated.get("job_id") or "")), updated)
    return updated


def _public_progress(state: dict[str, Any]) -> dict[str, Any]:
    child = state.get("child") if isinstance(state.get("child"), dict) else {}
    return {
        "phase": str(state.get("phase") or state.get("status") or "queued"),
        "outcome": str(state.get("outcome") or ""),
        "scope_type": str(state.get("scope_type") or ""),
        "scope_id": str(state.get("scope_id") or ""),
        "mode": str(state.get("mode") or ""),
        "child_kind": str(child.get("kind") or ""),
        "child_job_id": str(child.get("job_id") or ""),
        "child_progress": child.get("progress") if isinstance(child.get("progress"), dict) else {},
        "reason": str(state.get("reason") or ""),
        "elapsed_seconds": _elapsed_seconds(state),
        "readiness": state.get("readiness") if isinstance(state.get("readiness"), dict) else {},
    }


def start(
    root: Path,
    project_id: str,
    *,
    repo: str = "",
    source_id: str = "",
    mode: str = "full",
    resume_goal: str = "",
    origin_session_id: str = "",
) -> dict[str, Any]:
    pp = project_workspace.paths_for(root, project_id)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": project_id}
    normalized_mode = (mode or "full").strip().lower().replace("-", "_")
    if normalized_mode not in {"full", "local"}:
        return {"status": "rejected", "reason": "mode must be full or local"}
    scope_type, scope_id, error = _resolve_scope(root, project_id, repo, source_id)
    if error:
        return error
    blockers = _configuration_blockers(normalized_mode)
    if blockers:
        return {
            "status": "configuration_blocked",
            "outcome": "CONFIGURATION_BLOCKED",
            "project_id": project_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "mode": normalized_mode,
            "blockers": blockers,
            "retrieval": rag_backend.retrieval_status_snapshot(),
            "message": "Full repository readiness is blocked before any semantic upload/materialization begins.",
        }
    scope_key = _scope_key(scope_type, scope_id, normalized_mode)
    with _lock(root, project_id):
        for state in _latest_states(root, project_id):
            state = _refresh_stale_running_state(root, project_id, state)
            if str(state.get("status") or "") in {"queued", "running"} and str(state.get("scope_key") or "") == scope_key:
                return {
                    "status": "already_running",
                    "project_id": project_id,
                    "job": state,
                    "progress": _public_progress(state),
                    "recommended_poll_after_seconds": _poll_seconds(),
                    "next_poll_after": _iso_after(_poll_seconds()),
                }
        job_id = "rpr_" + uuid.uuid4().hex[:16]
        now = _now()
        state = {
            "schema_version": STATE_VERSION,
            "job_id": job_id,
            "project_id": project_id,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "scope_key": scope_key,
            "mode": normalized_mode,
            "semantic_authorized": normalized_mode == "full",
            "resume_goal": resume_goal[:4000],
            "origin_session_id": origin_session_id[:256],
            "status": "queued",
            "outcome": "PREPARATION_RUNNING",
            "phase": "planning",
            "pid": 0,
            "created_at": now,
            "started_at": "",
            "updated_at": now,
            "finished_at": "",
            "reason": "",
            "child": {},
            "readiness": {},
            "log_path": str(_log_path(root, project_id, job_id).relative_to(root)),
        }
        _write_json(_state_path(root, project_id, job_id), state)
        log_path = _log_path(root, project_id, job_id)
        with log_path.open("ab", buffering=0) as log_handle:
            env = None if normalized_mode == "full" else runtime_safety.credential_free_environment()
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", "--root", str(root), "--project", project_id, "--job-id", job_id],
                cwd=str(root), stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True, env=env,
            )
        state["pid"] = int(proc.pid)
        state["status"] = "running"
        state["started_at"] = _now()
        state["updated_at"] = state["started_at"]
        _write_json(_state_path(root, project_id, job_id), state)
    return {
        "status": "started",
        "project_id": project_id,
        "job": state,
        "progress": _public_progress(state),
        "recommended_poll_after_seconds": _poll_seconds(),
        "next_poll_after": _iso_after(_poll_seconds()),
        "message": "Repository preparation is owned by one detached parent job; structural, vector, and verification phases advance without model polling.",
    }


def status(root: Path, project_id: str, *, job_id: str = "", repo: str = "", source_id: str = "", mode: str = "full") -> dict[str, Any]:
    pp = project_workspace.paths_for(root, project_id)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": project_id}
    with _lock(root, project_id):
        if job_id:
            state = _read_json(_state_path(root, project_id, job_id))
            if not state:
                return {"status": "not_found", "project_id": project_id, "job_id": job_id}
        else:
            scope_type, scope_id, error = _resolve_scope(root, project_id, repo, source_id)
            if error:
                return error
            key = _scope_key(scope_type, scope_id, mode.strip().lower().replace("-", "_"))
            candidates = [row for row in _latest_states(root, project_id) if str(row.get("scope_key") or "") == key]
            if not candidates:
                return {"status": "not_found", "project_id": project_id, "reason": "no matching repository preparation job exists"}
            state = candidates[0]
        state = _refresh_stale_running_state(root, project_id, state)
    return {
        "status": "ok",
        "project_id": project_id,
        "job": state,
        "progress": _public_progress(state),
        "terminal": str(state.get("status") or "") in TERMINAL,
        "recommended_poll_after_seconds": _poll_seconds(),
        "next_poll_after": "" if str(state.get("status") or "") in TERMINAL else _iso_after(_poll_seconds()),
    }


def cancel(root: Path, project_id: str, *, job_id: str) -> dict[str, Any]:
    if not job_id:
        return {"status": "rejected", "reason": "job_id is required"}
    with _lock(root, project_id):
        state = _read_json(_state_path(root, project_id, job_id))
        if not state:
            return {"status": "not_found", "project_id": project_id, "job_id": job_id}
        state = _refresh_stale_running_state(root, project_id, state)
        if str(state.get("status") or "") not in {"queued", "running"}:
            return {"status": "not_running", "project_id": project_id, "job": state}
        child = state.get("child") if isinstance(state.get("child"), dict) else {}
        child_kind = str(child.get("kind") or "")
        child_job_id = str(child.get("job_id") or "")
        if child_job_id:
            if child_kind == "structural":
                code_index_jobs.cancel(root, project_id, job_id=child_job_id)
            elif child_kind == "vector":
                code_vector_jobs.cancel(root, project_id, job_id=child_job_id)
        try:
            os.kill(int(state.get("pid") or 0), signal.SIGTERM)
        except ProcessLookupError:
            pass
        state["status"] = "cancelled"
        state["outcome"] = "PRECONDITION_FAILED"
        state["reason"] = "repository preparation cancelled by explicit user request"
        state["finished_at"] = _now()
        state["updated_at"] = state["finished_at"]
        _write_json(_state_path(root, project_id, job_id), state)
    return {"status": "cancelled", "project_id": project_id, "job": state}


def _update(root: Path, project_id: str, job_id: str, **updates: Any) -> dict[str, Any]:
    path = _state_path(root, project_id, job_id)
    state = _read_json(path)
    if not state or str(state.get("status") or "") == "cancelled":
        return state
    state.update(updates)
    state["updated_at"] = _now()
    _write_json(path, state)
    return state


def _selector(state: dict[str, Any]) -> dict[str, str]:
    if str(state.get("scope_type") or "") == "source":
        return {"source_id": str(state.get("scope_id") or "")}
    return {"repo": str(state.get("scope_id") or "")}


def _wait_child(root: Path, project_id: str, parent_job_id: str, kind: str, child_job_id: str) -> dict[str, Any]:
    while True:
        parent = _read_json(_state_path(root, project_id, parent_job_id))
        if str(parent.get("status") or "") == "cancelled":
            return {"status": "cancelled"}
        result = (
            code_index_jobs.status(root, project_id, job_id=child_job_id)
            if kind == "structural"
            else code_vector_jobs.status(root, project_id, job_id=child_job_id)
        )
        child_state = result.get("job") if isinstance(result.get("job"), dict) else {}
        progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
        _update(root, project_id, parent_job_id, child={"kind": kind, "job_id": child_job_id, "status": child_state.get("status"), "progress": progress})
        status_value = str(child_state.get("status") or "")
        if status_value in {"completed", "failed", "cancelled", "interrupted"}:
            return result
        sleep_for = int(result.get("retry_after_seconds") or result.get("recommended_poll_after_seconds") or _poll_seconds())
        time.sleep(max(1, min(sleep_for, 120)))


def _fail(root: Path, project_id: str, job_id: str, reason: str, *, blocked: bool = False, outcome: str = "PRECONDITION_FAILED") -> int:
    now = _now()
    _update(
        root, project_id, job_id,
        status="blocked" if blocked else "failed", outcome=outcome,
        reason=reason[:2000], phase="blocked" if blocked else "failed",
        finished_at=now,
    )
    return 2 if blocked else 1


def _worker(root: Path, project_id: str, job_id: str) -> int:
    state_path = _state_path(root, project_id, job_id)
    state = _read_json(state_path)
    if not state:
        return 2
    paths = SimpleNamespace(root=root)
    selector = _selector(state)
    index_selector = {"source": selector["source_id"]} if "source_id" in selector else selector
    try:
        _update(root, project_id, job_id, phase="structural_check")
        passive = code_search.index_status(paths, project_id, **index_selector)
        freshness = passive.get("freshness") if isinstance(passive.get("freshness"), dict) else {}
        if not bool(freshness.get("lexical_current")):
            _update(root, project_id, job_id, phase="structural_refresh")
            started = code_index_jobs.start(root, project_id, **selector)
            if str(started.get("status") or "") not in {"started", "already_running"}:
                return _fail(root, project_id, job_id, f"structural refresh could not start: {started.get('reason') or started.get('status')}")
            child = started.get("job") if isinstance(started.get("job"), dict) else {}
            child_job_id = str(child.get("job_id") or "")
            _update(root, project_id, job_id, child={"kind": "structural", "job_id": child_job_id, "status": child.get("status"), "progress": started.get("progress") or {}})
            waited = _wait_child(root, project_id, job_id, "structural", child_job_id)
            child_state = waited.get("job") if isinstance(waited.get("job"), dict) else {}
            if str(child_state.get("status") or "") != "completed":
                return _fail(root, project_id, job_id, f"structural refresh {child_job_id} ended {child_state.get('status')}: {child_state.get('reason') or ''}")

        _update(root, project_id, job_id, phase="structural_verify", child={})
        verified = code_search.index_status(paths, project_id, deep_verify=True, verify_qdrant=False, **index_selector)
        vfresh = verified.get("freshness") if isinstance(verified.get("freshness"), dict) else {}
        if not bool(vfresh.get("lexical_current")):
            return _fail(root, project_id, job_id, "structural/FTS verification did not establish lexical_current=true")

        if str(state.get("mode") or "full") == "local":
            now = _now()
            _update(root, project_id, job_id, status="completed", outcome="LOCAL_READY", phase="complete", finished_at=now,
                    readiness={"structural": verified, "semantic_requested": False})
            return 0

        blockers = _configuration_blockers("full")
        if blockers:
            return _fail(root, project_id, job_id, ",".join(blockers), blocked=True, outcome="CONFIGURATION_BLOCKED")

        _update(root, project_id, job_id, phase="vector_check")
        passive = code_search.index_status(paths, project_id, **index_selector)
        pfresh = passive.get("freshness") if isinstance(passive.get("freshness"), dict) else {}
        if not bool(pfresh.get("vector_current")):
            _update(root, project_id, job_id, phase="vector_refresh")
            started = code_vector_jobs.start(root, project_id, **selector)
            if str(started.get("status") or "") not in {"started", "already_running"}:
                return _fail(root, project_id, job_id, f"vector refresh could not start: {started.get('reason') or started.get('status')}")
            child = started.get("job") if isinstance(started.get("job"), dict) else {}
            child_job_id = str(child.get("job_id") or "")
            _update(root, project_id, job_id, child={"kind": "vector", "job_id": child_job_id, "status": child.get("status"), "progress": started.get("progress") or {}})
            waited = _wait_child(root, project_id, job_id, "vector", child_job_id)
            child_state = waited.get("job") if isinstance(waited.get("job"), dict) else {}
            if str(child_state.get("status") or "") != "completed":
                progress = waited.get("progress") if isinstance(waited.get("progress"), dict) else {}
                reason = str(progress.get("reason") or child_state.get("reason") or "")
                return _fail(root, project_id, job_id, f"vector refresh {child_job_id} ended {child_state.get('status')}: {reason}", blocked=True, outcome="PRECONDITION_FAILED")

        _update(root, project_id, job_id, phase="final_verify", child={})
        verified_full = code_search.index_status(paths, project_id, deep_verify=True, verify_qdrant=True, **index_selector)
        final_fresh = verified_full.get("freshness") if isinstance(verified_full.get("freshness"), dict) else {}
        if not bool(final_fresh.get("lexical_current")) or not bool(final_fresh.get("vector_current")):
            return _fail(root, project_id, job_id, "final source/vector membership verification is not current")

        _update(root, project_id, job_id, phase="backend_probe")
        probe = rag_backend.probe_retrieval(probe_qdrant=True, probe_embedding=True, probe_reranker=True, timeout_seconds=10.0)
        snapshot = rag_backend.retrieval_status_snapshot()
        embedding = snapshot.get("embedding") if isinstance(snapshot.get("embedding"), dict) else {}
        rerank = snapshot.get("rerank") if isinstance(snapshot.get("rerank"), dict) else {}
        qdrant = probe.get("qdrant") if isinstance(probe.get("qdrant"), dict) else {}
        ep = probe.get("embedding") if isinstance(probe.get("embedding"), dict) else {}
        rp = probe.get("rerank") if isinstance(probe.get("rerank"), dict) else {}
        healthy = (
            bool(qdrant.get("available"))
            and ep.get("status") == "ok"
            and rp.get("status") == "ok"
            and bool(embedding.get("configuration_ready"))
            and bool(rerank.get("enabled"))
            and bool(rerank.get("configuration_ready"))
        )
        if not healthy:
            return _fail(root, project_id, job_id, "final retrieval backend probe/configuration did not establish full readiness", blocked=True, outcome="CONFIGURATION_BLOCKED")

        now = _now()
        _update(
            root, project_id, job_id, status="completed", outcome="FULL_READY", phase="complete", finished_at=now,
            readiness={"index_verify": verified_full, "retrieval": snapshot, "probe": probe},
        )
        return 0
    except BaseException as exc:
        current = _read_json(state_path)
        if str(current.get("status") or "") == "cancelled":
            return 0
        return _fail(root, project_id, job_id, f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--root", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    if not args.worker:
        parser.error("--worker is required")
    return _worker(Path(args.root).resolve(), project_workspace.clean_project_id(args.project), args.job_id)


if __name__ == "__main__":
    raise SystemExit(main())
