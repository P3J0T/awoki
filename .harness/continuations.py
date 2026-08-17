"""Durable, session-scoped continuation scheduling for detached Awoki jobs.

The store contains bounded operational metadata only. It never persists tool-output
bodies, credentials, conversation transcripts, or private reasoning. Detached job
status is polled locally by the OpenCode continuity plugin; the model is only resumed
when the waited job reaches a terminal state (or when the continuation is blocked).
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import project_workspace

SCHEMA_VERSION = 2
MIN_WAIT_SECONDS = 2
MAX_WAIT_SECONDS = 60 * 60
MAX_LIFETIME_SECONDS = 48 * 60 * 60
CLAIM_LEASE_SECONDS = 10 * 60
MAX_AUTO_RESUME_ATTEMPTS = 3
_ALLOWED_WORKFLOWS = {"repository-readiness", "generic"}
_ALLOWED_WAIT_TOOLS = {"code_index_refresh_status", "code_vector_refresh_status", "repository_prepare_status"}
_TERMINAL_JOB_STATES = {"completed", "blocked", "failed", "cancelled", "interrupted"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9._:/-]+")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clean_id(value: str, *, max_len: int = 300) -> str:
    cleaned = _SAFE_ID.sub("-", str(value or "").strip()).strip("-._:/")
    return cleaned[:max_len]


def _clean_text(value: str, *, max_len: int) -> str:
    return " ".join(str(value or "").split())[:max_len]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _state(root: Path, session_id: str) -> tuple[Path, dict[str, Any]]:
    path = project_workspace.session_state_path(root, session_id)
    state = _read_json(path)
    if not state:
        state = {
            "session_id": session_id,
            "status": "unattached",
            "created_at": _now(),
            "last_activity_at": _now(),
        }
    state.setdefault("session_id", session_id)
    return path, state


def _public(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "schema_version", "continuation_id", "status", "workflow", "phase", "scope_kind",
        "project_id", "origin_project_id", "repo", "source_id", "wait_tool", "wait_job_id",
        "not_before", "deadline_at", "poll_after_seconds", "poll_count", "last_job_status",
        "last_progress", "next_action", "resume_goal", "auto_resume", "generation", "attempts",
        "claimed_at", "lease_until", "created_at", "updated_at", "blocked_reason",
    )
    return {key: record.get(key) for key in keys if key in record}


def continuation_id(session_id: str, generation: int) -> str:
    return "cont_" + hashlib.sha256(f"{session_id}|{generation}".encode()).hexdigest()[:16]


def _normalize_project(root: Path, session_id: str, project_id: str) -> tuple[str, str | None]:
    explicit = _clean_id(project_id, max_len=120)
    if explicit:
        try:
            explicit = project_workspace.clean_project_id(explicit)
        except ValueError as exc:
            return "", str(exc)
        if not project_workspace.project_exists(root, explicit):
            return "", f"project does not exist: {explicit}"
        return explicit, None
    current = str(project_workspace.current_project_id(root, session_id=session_id) or "")
    return current, None


def schedule(
    root: Path,
    session_id: str,
    *,
    workflow: str,
    phase: str,
    wait_tool: str,
    wait_job_id: str,
    wait_seconds: int,
    project_id: str = "",
    repo: str = "",
    source_id: str = "",
    next_action: str = "",
    resume_goal: str = "",
    auto_resume: bool = True,
) -> dict[str, Any]:
    if not str(session_id or "").strip():
        return {"status": "rejected", "reason": "missing_session_id"}
    workflow_clean = _clean_id(workflow or "generic", max_len=80) or "generic"
    if workflow_clean not in _ALLOWED_WORKFLOWS:
        return {"status": "rejected", "reason": f"unsupported workflow: {workflow_clean}"}
    wait_tool_clean = _clean_id(wait_tool, max_len=120)
    if wait_tool_clean not in _ALLOWED_WAIT_TOOLS:
        return {"status": "rejected", "reason": f"unsupported wait tool: {wait_tool_clean}"}
    wait_job_clean = _clean_id(wait_job_id, max_len=240)
    if not wait_job_clean:
        return {"status": "rejected", "reason": "wait_job_id is required"}
    explicit_project, project_error = _normalize_project(root, session_id, project_id)
    if project_error:
        return {"status": "rejected", "reason": project_error}
    if not explicit_project:
        return {
            "status": "rejected",
            "reason": (
                "detached structural/vector continuation requires an existing managed project; "
                "an explicitly named managed project may be used while the session is unattached, "
                "but true ad-hoc/session-only work must not silently create or materialize managed indexes"
            ),
        }
    seconds = max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, int(wait_seconds or MIN_WAIT_SECONDS)))
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        prior = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        same_chain = (
            str(prior.get("workflow") or "") == workflow_clean
            and str(prior.get("project_id") or "") == explicit_project
            and str(prior.get("status") or "") not in {"done", "cancelled", "blocked"}
        )
        generation = int(prior.get("generation") or 0) + 1
        now = _now_dt()
        created_at = str(prior.get("created_at") or _now()) if same_chain else _now()
        deadline = _parse_ts(str(prior.get("deadline_at") or "")) if same_chain else None
        # An active chain never earns a fresh lifetime by being rescheduled. Preserve
        # even an already-expired deadline so poll/claim can fail closed. Only a
        # genuinely new chain receives a new lifetime.
        if deadline is None:
            deadline = now + timedelta(seconds=MAX_LIFETIME_SECONDS)
        poll_count = int(prior.get("poll_count") or 0) if same_chain else 0
        attempts = int(prior.get("attempts") or 0) if same_chain else 0
        origin_project = str(prior.get("origin_project_id") or "") if same_chain else str(project_workspace.current_project_id(root, session_id=session_id) or "")
        record = {
            "schema_version": SCHEMA_VERSION,
            "continuation_id": continuation_id(session_id, generation),
            "status": "waiting",
            "workflow": workflow_clean,
            "phase": _clean_id(phase, max_len=120),
            "scope_kind": "managed_project",
            "project_id": explicit_project,
            "origin_project_id": origin_project,
            "repo": _clean_id(repo, max_len=160),
            "source_id": _clean_id(source_id, max_len=160),
            "wait_tool": wait_tool_clean,
            "wait_job_id": wait_job_clean,
            "not_before": (now + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z"),
            "deadline_at": deadline.isoformat().replace("+00:00", "Z"),
            "poll_after_seconds": seconds,
            "poll_count": poll_count,
            "last_job_status": "",
            "last_progress": {},
            "next_action": _clean_text(next_action, max_len=1_000),
            "resume_goal": _clean_text(resume_goal, max_len=1_000),
            "auto_resume": bool(auto_resume),
            "generation": generation,
            "attempts": attempts,
            "claimed_at": "",
            "lease_until": "",
            "blocked_reason": "",
            "created_at": created_at,
            "updated_at": _now(),
        }
        state["continuation"] = record
        _atomic_write_json(path, state)
    return {"status": "scheduled", "session_id": session_id, "continuation": _public(record)}


def status(root: Path, session_id: str) -> dict[str, Any]:
    if not str(session_id or "").strip():
        return {"status": "rejected", "reason": "missing_session_id"}
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not record:
            return {"status": "none", "session_id": session_id}
        current_project = str(project_workspace.current_project_id(root, session_id=session_id) or "")
        expected = str(record.get("project_id") or "")
        return {
            "status": "ok",
            "session_id": session_id,
            "current_project_id": current_project,
            "scope_conflict": bool(expected and current_project and current_project != expected),
            "continuation": _public(record),
        }


def _poll_job(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    project_id = str(record.get("project_id") or "")
    if not project_id:
        return {"status": "rejected", "reason": "wait job has no managed project scope"}
    job_id = str(record.get("wait_job_id") or "")
    repo = str(record.get("repo") or "")
    source_id = str(record.get("source_id") or "")
    wait_tool = str(record.get("wait_tool") or "")
    if wait_tool == "code_index_refresh_status":
        import code_index_jobs
        return code_index_jobs.status(root, project_id, job_id=job_id, repo=repo, source_id=source_id)
    if wait_tool == "code_vector_refresh_status":
        import code_vector_jobs
        return code_vector_jobs.status(root, project_id, job_id=job_id, repo=repo, source_id=source_id)
    if wait_tool == "repository_prepare_status":
        import repository_prepare_jobs
        return repository_prepare_jobs.status(root, project_id, job_id=job_id, repo=repo, source_id=source_id)
    return {"status": "rejected", "reason": f"unsupported wait tool: {wait_tool}"}


def poll_due(root: Path, session_id: str) -> dict[str, Any]:
    """Poll a waited detached job without invoking the model.

    Running jobs are rescheduled locally. Terminal jobs become ``ready`` so the
    OpenCode plugin can resume the model once the session is idle.
    """
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not record:
            return {"status": "none", "session_id": session_id}
        if not record.get("auto_resume"):
            return {"status": "manual", "session_id": session_id, "continuation": _public(record)}
        state_name = str(record.get("status") or "")
        if state_name in {"done", "cancelled", "blocked", "ready", "claimed"}:
            return {"status": state_name, "session_id": session_id, "continuation": _public(record)}
        now = _now_dt()
        deadline = _parse_ts(str(record.get("deadline_at") or ""))
        if deadline and now >= deadline:
            record["status"] = "blocked"
            record["blocked_reason"] = "continuation_deadline_exceeded"
            record["updated_at"] = _now()
            state["continuation"] = record
            _atomic_write_json(path, state)
            return {"status": "blocked", "session_id": session_id, "continuation": _public(record)}
        not_before = _parse_ts(str(record.get("not_before") or "")) or now
        if now < not_before:
            return {
                "status": "waiting",
                "session_id": session_id,
                "seconds_remaining": max(1, int((not_before - now).total_seconds())),
                "continuation": _public(record),
            }
        generation = int(record.get("generation") or 0)

    # Job status may acquire its own lock; do not hold the session lock around it.
    observed = _poll_job(root, record)

    with _lock(path):
        _, state = _state(root, session_id)
        current = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not current:
            return {"status": "none", "session_id": session_id}
        if int(current.get("generation") or 0) != generation:
            return {"status": "superseded", "session_id": session_id, "continuation": _public(current)}
        job = observed.get("job") if isinstance(observed.get("job"), dict) else {}
        job_status = str(job.get("status") or observed.get("status") or "unknown")
        progress = observed.get("progress") if isinstance(observed.get("progress"), dict) else {}
        # Keep only bounded progress metrics; no source text or tool-output body.
        allowed_progress = {
            key: progress.get(key)
            for key in (
                "phase", "progress_percent", "elapsed_seconds", "files_total", "files_processed",
                "files_parsed", "files_reused", "files_removed", "chunks_total", "chunks_ready",
                "vectors_ready", "vectors_remaining", "vectors_persisted", "batches_total",
                "batches_completed", "current_repository", "current_source", "current_path",
                "outcome", "scope_type", "scope_id", "mode", "child_kind", "child_job_id", "reason",
            )
            if key in progress
        }
        current["last_job_status"] = job_status
        current["last_progress"] = allowed_progress
        current["poll_count"] = int(current.get("poll_count") or 0) + 1
        current["updated_at"] = _now()
        if job_status in _TERMINAL_JOB_STATES or str(observed.get("status") or "") in {"not_found", "rejected"}:
            current["status"] = "ready"
            current["not_before"] = ""
            current["poll_after_seconds"] = 0
        else:
            recommended = int(observed.get("retry_after_seconds") or observed.get("recommended_poll_after_seconds") or current.get("poll_after_seconds") or 30)
            recommended = max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, recommended))
            current["status"] = "waiting"
            current["poll_after_seconds"] = recommended
            current["not_before"] = (_now_dt() + timedelta(seconds=recommended)).isoformat().replace("+00:00", "Z")
        state["continuation"] = current
        _atomic_write_json(path, state)
        return {
            "status": str(current.get("status") or "waiting"),
            "session_id": session_id,
            "job_observation": {
                "status": str(observed.get("status") or ""),
                "job_status": job_status,
                "poll_too_soon": bool(observed.get("poll_too_soon")),
            },
            "continuation": _public(current),
        }


def claim_due(root: Path, session_id: str) -> dict[str, Any]:
    """Atomically claim one terminal/ready continuation for an idle session."""
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not record:
            return {"status": "none", "session_id": session_id}
        if not record.get("auto_resume"):
            return {"status": "manual", "session_id": session_id, "continuation": _public(record)}
        state_name = str(record.get("status") or "")
        if state_name in {"done", "cancelled", "blocked"}:
            return {"status": state_name, "session_id": session_id, "continuation": _public(record)}
        current_project = str(project_workspace.current_project_id(root, session_id=session_id) or "")
        expected_project = str(record.get("project_id") or "")
        if expected_project and current_project and current_project != expected_project:
            return {
                "status": "scope_conflict",
                "session_id": session_id,
                "current_project_id": current_project,
                "continuation": _public(record),
            }
        now = _now_dt()
        deadline = _parse_ts(str(record.get("deadline_at") or ""))
        if deadline and now >= deadline:
            record["status"] = "blocked"
            record["blocked_reason"] = "continuation_deadline_exceeded"
            record["updated_at"] = _now()
            state["continuation"] = record
            _atomic_write_json(path, state)
            return {"status": "blocked", "session_id": session_id, "continuation": _public(record)}
        lease_until = _parse_ts(str(record.get("lease_until") or ""))
        if state_name == "claimed" and lease_until and now < lease_until:
            return {"status": "leased", "session_id": session_id, "continuation": _public(record)}
        if state_name == "claimed":
            # A process/plugin may have died after claiming the continuation. Once
            # the bounded lease expires, make it claimable again rather than leaving
            # durable state stuck in ``claimed`` forever.
            record["status"] = "ready"
            record["claimed_at"] = ""
            record["lease_until"] = ""
            record["not_before"] = ""
            record["updated_at"] = _now()
            state["continuation"] = record
            _atomic_write_json(path, state)
            state_name = "ready"
        if state_name != "ready":
            return {"status": "waiting", "session_id": session_id, "continuation": _public(record)}
        retry_not_before = _parse_ts(str(record.get("not_before") or ""))
        if retry_not_before and now < retry_not_before:
            return {
                "status": "waiting",
                "session_id": session_id,
                "seconds_remaining": max(1, int((retry_not_before - now).total_seconds())),
                "continuation": _public(record),
            }
        attempts = int(record.get("attempts") or 0)
        if attempts >= MAX_AUTO_RESUME_ATTEMPTS:
            record["status"] = "blocked"
            record["blocked_reason"] = "auto_resume_attempt_limit"
            record["updated_at"] = _now()
            state["continuation"] = record
            _atomic_write_json(path, state)
            return {"status": "blocked", "session_id": session_id, "continuation": _public(record)}
        record["status"] = "claimed"
        record["attempts"] = attempts + 1
        record["claimed_at"] = _now()
        record["lease_until"] = (now + timedelta(seconds=CLAIM_LEASE_SECONDS)).isoformat().replace("+00:00", "Z")
        record["updated_at"] = _now()
        state["continuation"] = record
        _atomic_write_json(path, state)
        return {"status": "due", "session_id": session_id, "continuation": _public(record)}


def release(root: Path, session_id: str, *, generation: int, retry_seconds: int = 60, reason: str = "resume_failed") -> dict[str, Any]:
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not record:
            return {"status": "none", "session_id": session_id}
        if int(record.get("generation") or 0) != int(generation):
            return {"status": "superseded", "session_id": session_id, "continuation": _public(record)}
        seconds = max(MIN_WAIT_SECONDS, min(300, int(retry_seconds or 60)))
        record["status"] = "ready"
        record["lease_until"] = ""
        record["claimed_at"] = ""
        record["blocked_reason"] = _clean_text(reason, max_len=200)
        record["updated_at"] = _now()
        # retry delay is represented separately so plugin timers do not spin.
        record["not_before"] = (_now_dt() + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        state["continuation"] = record
        _atomic_write_json(path, state)
        return {"status": "released", "session_id": session_id, "retry_after_seconds": seconds, "continuation": _public(record)}


def finalize(root: Path, session_id: str, *, reason: str = "completed") -> dict[str, Any]:
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not record:
            return {"status": "none", "session_id": session_id}
        record["status"] = "done"
        record["blocked_reason"] = _clean_text(reason, max_len=200)
        record["lease_until"] = ""
        record["updated_at"] = _now()
        state["continuation"] = record
        _atomic_write_json(path, state)
        return {"status": "finalized", "session_id": session_id, "continuation": _public(record)}


def cancel(root: Path, session_id: str, *, reason: str = "cancelled") -> dict[str, Any]:
    path = project_workspace.session_state_path(root, session_id)
    with _lock(path):
        _, state = _state(root, session_id)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        if not record:
            return {"status": "none", "session_id": session_id}
        record["status"] = "cancelled"
        record["blocked_reason"] = _clean_text(reason, max_len=200)
        record["lease_until"] = ""
        record["updated_at"] = _now()
        state["continuation"] = record
        _atomic_write_json(path, state)
        return {"status": "cancelled", "session_id": session_id, "continuation": _public(record)}


def pending(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    session_root = project_workspace.sessions_dir(root)
    if not session_root.exists():
        return rows
    for path in sorted(session_root.glob("*.json")):
        state = _read_json(path)
        record = state.get("continuation") if isinstance(state.get("continuation"), dict) else {}
        sid = str(state.get("session_id") or "")
        if not sid or not record or not record.get("auto_resume"):
            continue
        if str(record.get("status") or "") in {"done", "cancelled", "blocked"}:
            continue
        rows.append({"session_id": sid, "continuation": _public(record)})
    return rows
