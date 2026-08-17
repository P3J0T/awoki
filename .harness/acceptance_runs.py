from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import code_search
import evidence_store
import project_workspace
import safety
import work_ledger

SCHEMA = "awoki-acceptance-run/v4"
V3_SCHEMA = "awoki-acceptance-run/v3"
PREVIOUS_SCHEMA = "awoki-acceptance-run/v2"
LEGACY_SCHEMA = "awoki-acceptance-run/v1"
SUPPORTED_SCHEMAS = {SCHEMA, V3_SCHEMA, PREVIOUS_SCHEMA, LEGACY_SCHEMA}
MAX_TESTS = 64
MAX_EVIDENCE_KEYS = 80
MAX_STRING = 1200
MAX_EVIDENCE_REFS = 16
MAX_OBSERVATION_STRING = 800
MAX_TOOL_EVENTS_PER_TEST = 96
MAX_CONTRACT_ITEMS = 32
MAX_COMPACTION_EVENTS = 16
MAX_ATTEMPTS_PER_TEST = 8
_OBSERVABLE_ORCHESTRATION_TOOLS = {"acceptance_run_start", "acceptance_run_status", "acceptance_run_next"}
_CONTROL_ORCHESTRATION_TOOLS = {"acceptance_run_record", "acceptance_run_record_invariant", "acceptance_run_finalize"}
_OUTCOMES = {
    "pass", "fail", "blocked", "inconclusive", "incomplete", "not_applicable",
    "backend_degraded", "protocol_deviation",
}
_FORBIDDEN_EVIDENCE_KEYS = {
    "source", "source_text", "source_preview", "preview", "snippet", "content", "raw_output", "tool_output",
    "raw_payload", "payload", "transcript", "message_body", "response_body",
    "corrective_budget", "corrective_budget_total", "corrective_budget_used", "corrective_budget_remaining",
}
_FORBIDDEN_BLOB_MARKERS = ("source_preview", "tool_output", "mcp_output", "transcript")
_CANDIDATE_METRIC_KEYS = {
    "fts_rank", "qdrant_rank", "fused_rank", "pre_rerank_rank", "post_refinement_rank",
    "composed_rank", "rerank_rank", "rerank_score", "tei_score", "final_rank", "final_score",
}


class LegacyAcceptanceMigrationError(RuntimeError):
    pass


def _now() -> str:
    return project_workspace.now_ts()


def _clean_id(value: str, *, prefix: str = "") -> str:
    clean = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "").strip()).strip("-._:")
    if clean:
        return clean[:160]
    return prefix + uuid.uuid4().hex[:16]


def _session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _session_path(root: Path, session_id: str) -> Path:
    return project_workspace.state_dir(root) / "acceptance-sessions" / f"{_session_key(session_id)}.json"


def _dir(root: Path, project_id: str) -> Path:
    return project_workspace.paths_for(root, project_id).artifacts_dir / "acceptance"


def _path(root: Path, project_id: str, run_id: str) -> Path:
    return _dir(root, project_id) / f"{_clean_id(run_id)}.json"


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


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Legacy bounded sanitizer retained for reading/migrating v1 records."""
    if depth > 4:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        redacted, _ = safety.redact_text(value[:MAX_STRING])
        return redacted
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, nested in list(value.items())[:MAX_EVIDENCE_KEYS]:
            clean_key = str(key)[:120]
            if clean_key.lower() in _FORBIDDEN_EVIDENCE_KEYS:
                result[clean_key] = "<omitted: raw/source text is not accepted in acceptance evidence>"
            else:
                result[clean_key] = _sanitize(nested, depth=depth + 1)
        return result
    return str(value)[:MAX_STRING]


def _observation_key_forbidden(key: str) -> bool:
    lower = key.lower()
    return (
        lower in _FORBIDDEN_EVIDENCE_KEYS
        or lower.startswith("raw_")
        or lower.endswith("_raw")
        or any(marker in lower for marker in _FORBIDDEN_BLOB_MARKERS)
    )


def _candidate_metric_alias(key: str) -> bool:
    lower = key.lower()
    if lower in _CANDIDATE_METRIC_KEYS:
        return True
    return any(lower.endswith("_" + metric) for metric in _CANDIDATE_METRIC_KEYS)


def _sanitize_observations(
    value: dict[str, Any] | None, *, forbid_candidate_metrics: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """Validate the compact control-plane observation map.

    Rich/raw Awoki tool evidence belongs in evidence_store artifacts referenced by
    evidence_ref.  The ledger accepts only small scalar facts, not transcript/source
    blobs or arbitrary nested payloads.
    """
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        return {}, ["evidence must be an object of bounded scalar observations"]
    result: dict[str, Any] = {}
    errors: list[str] = []
    for key, nested in list(value.items())[:MAX_EVIDENCE_KEYS]:
        clean_key = str(key)[:120]
        if _observation_key_forbidden(clean_key):
            errors.append(f"forbidden evidence field: {clean_key}")
            continue
        if _candidate_metric_alias(clean_key) and clean_key.lower() not in _CANDIDATE_METRIC_KEYS:
            errors.append(f"candidate-specific metric aliases are not accepted; use canonical candidate_ids: {clean_key}")
            continue
        if forbid_candidate_metrics and clean_key.lower() in _CANDIDATE_METRIC_KEYS:
            errors.append(f"candidate metric must come from canonical candidate evidence, not observation field: {clean_key}")
            continue
        if isinstance(nested, (dict, tuple, set)):
            errors.append(f"nested evidence is not accepted in compact observations: {clean_key}")
            continue
        if isinstance(nested, list):
            if len(nested) > 32 or any(not (item is None or isinstance(item, (bool, int, float, str))) for item in nested):
                errors.append(f"evidence list must contain at most 32 scalar values: {clean_key}")
                continue
            cleaned_list: list[Any] = []
            rejected = False
            for item in nested:
                if isinstance(item, str):
                    if len(item) > MAX_OBSERVATION_STRING or item.count("\n") > 4:
                        errors.append(f"evidence string is too large/multiline for compact observations: {clean_key}")
                        rejected = True
                        break
                    item, _ = safety.redact_text(item)
                cleaned_list.append(item)
            if not rejected:
                result[clean_key] = cleaned_list
            continue
        if isinstance(nested, str):
            if len(nested) > MAX_OBSERVATION_STRING or nested.count("\n") > 4:
                errors.append(f"evidence string is too large/multiline for compact observations: {clean_key}")
                continue
            nested, _ = safety.redact_text(nested)
            result[clean_key] = nested
            continue
        if nested is None or isinstance(nested, (bool, int, float)):
            result[clean_key] = nested
            continue
        errors.append(f"unsupported evidence value type for compact observation: {clean_key}")
    return result, errors




def _safe_text(value: Any, limit: int = MAX_STRING) -> str:
    redacted, _ = safety.redact_text(str(value or "")[:limit])
    return redacted


def _safe_compact_text(value: Any, limit: int = MAX_OBSERVATION_STRING) -> str:
    text = str(value or "").strip()
    if len(text) > limit or text.count("\n") > 4:
        raise ValueError(f"compact acceptance text exceeds {limit} characters or 4 newlines")
    redacted, _ = safety.redact_text(text)
    return redacted

def _scope_snapshot(root: Path, project_id: str, *, repo: str = "", source_id: str = "") -> dict[str, Any]:
    resolved = project_workspace.resolve_project_source(
        root, project_id, source_id=source_id, repo_id=repo, require_unique=True
    )
    if resolved.get("status") != "ok":
        return {"status": "scope_error", "error": resolved}
    rid = str(resolved.get("repo_id") or "")
    sid = str(resolved.get("source_id") or "")
    index = code_search.index_status(SimpleNamespace(root=root), project_id, repo=rid, source=sid)
    branch = dict(index.get("active_branch") or {})
    freshness = dict(index.get("freshness") or {})
    return {
        "status": "ok",
        "project_id": project_id,
        "repo": rid,
        "source_id": sid,
        "source_type": str(resolved.get("source_type") or ""),
        "revision": {
            "branch_key": str(branch.get("branch_key") or ""),
            "commit_sha": str(branch.get("commit_sha") or ""),
            "revision_key": str(branch.get("revision_key") or ""),
            "content_identity": str(branch.get("content_identity") or ""),
            "dirty": bool(branch.get("dirty")),
        },
        "repository_assurance": str(index.get("repository_assurance") or ""),
        "freshness": {
            "lexical_current": bool(freshness.get("lexical_current")),
            "vector_current": bool(freshness.get("vector_current")),
            "membership_hash": str(freshness.get("indexed_membership_hash") or ""),
        },
    }


def scope_snapshot(root: Path, project_id: str, *, repo: str = "", source_id: str = "") -> dict[str, Any]:
    return _scope_snapshot(root, project_id, repo=repo, source_id=source_id)


def _scope_identity(scope: dict[str, Any]) -> dict[str, Any]:
    revision = dict(scope.get("revision") or {})
    freshness = dict(scope.get("freshness") or {})
    return {
        "project_id": str(scope.get("project_id") or ""),
        "repo": str(scope.get("repo") or ""),
        "source_id": str(scope.get("source_id") or ""),
        "source_type": str(scope.get("source_type") or ""),
        "branch_key": str(revision.get("branch_key") or ""),
        "commit_sha": str(revision.get("commit_sha") or ""),
        "revision_key": str(revision.get("revision_key") or ""),
        "content_identity": str(revision.get("content_identity") or ""),
        "dirty": bool(revision.get("dirty")),
        "membership_hash": str(freshness.get("membership_hash") or ""),
    }


def scope_identity(scope: dict[str, Any]) -> dict[str, Any]:
    return _scope_identity(scope)


def _scope_guard(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    expected = dict(state.get("scope") or {})
    current = _scope_snapshot(
        root,
        str(state.get("project_id") or ""),
        repo=str(expected.get("repo") or ""),
        source_id=str(expected.get("source_id") or ""),
    )
    if current.get("status") != "ok":
        return {"status": "scope_error", "expected": _scope_identity(expected), "current": current}
    expected_identity = _scope_identity(expected)
    current_identity = _scope_identity(current)
    if expected_identity != current_identity:
        return {
            "status": "scope_drift",
            "reason": "Acceptance source/revision or published membership changed after the run started.",
            "expected": expected_identity,
            "current": current_identity,
        }
    return {"status": "ok", "identity": current_identity}


def _safe_string_list(value: Any, *, limit: int = MAX_CONTRACT_ITEMS, width: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_safe_text(item, width) for item in value[:limit] if str(item).strip()]


def _sanitize_requirement(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    field = re.sub(r"[^A-Za-z0-9_.:-]+", "", str(value.get("field") or "").strip())[:160]
    op = str(value.get("op") or "eq").strip().lower()
    if not field or op not in {"eq", "ne", "gte", "lte", "gt", "lt", "truthy", "falsy", "exists", "not_exists", "in"}:
        return None
    raw_expected = value.get("value")
    if isinstance(raw_expected, list):
        expected: Any = [item for item in raw_expected[:32] if isinstance(item, (str, int, float, bool)) or item is None]
    elif isinstance(raw_expected, (str, int, float, bool)) or raw_expected is None:
        expected = raw_expected
    else:
        return None
    return {"field": field, "op": op, "value": expected}


def _sanitize_test_plan(value: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in (value or [])[:MAX_TESTS]:
        if not isinstance(raw, dict):
            continue
        test_id = _clean_id(str(raw.get("test_id") or ""))
        if not test_id or test_id in seen:
            continue
        seen.add(test_id)
        objective = _safe_text(raw.get("objective") or "", 1200)
        allowed = _safe_string_list(raw.get("allowed_actions"), limit=24)
        forbidden = _safe_string_list(raw.get("forbidden_actions"), limit=24)
        required_interfaces = _safe_string_list(raw.get("required_interfaces"))
        required_orchestration_interfaces = _safe_string_list(raw.get("required_orchestration_interfaces"))
        moved_orchestration = [tool for tool in required_interfaces if tool in _OBSERVABLE_ORCHESTRATION_TOOLS]
        if moved_orchestration:
            required_interfaces = [tool for tool in required_interfaces if tool not in _OBSERVABLE_ORCHESTRATION_TOOLS]
            required_orchestration_interfaces = list(dict.fromkeys(required_orchestration_interfaces + moved_orchestration))
        required_observations = _safe_string_list(raw.get("required_observations"), width=160)
        forbidden_tool_classes = _safe_string_list(raw.get("forbidden_tool_classes"), width=80)
        native_policy_declared = "allowed_native_tools" in raw or "native_tool_limits" in raw
        allowed_native_tools = _safe_string_list(raw.get("allowed_native_tools"), width=120)
        limits: dict[str, int] = {}
        raw_limits = raw.get("native_tool_limits")
        if isinstance(raw_limits, dict):
            for key, count in list(raw_limits.items())[:MAX_CONTRACT_ITEMS]:
                clean_key = _safe_text(key, 120).strip()
                if not clean_key:
                    continue
                try:
                    limits[clean_key] = max(0, min(32, int(count)))
                except (TypeError, ValueError):
                    continue
        interface_limits: dict[str, int] = {}
        raw_interface_limits = raw.get("interface_limits")
        if isinstance(raw_interface_limits, dict):
            for key, count in list(raw_interface_limits.items())[:MAX_CONTRACT_ITEMS]:
                clean_key = _safe_text(key, 120).strip()
                if not clean_key:
                    continue
                try:
                    interface_limits[clean_key] = max(0, min(64, int(count)))
                except (TypeError, ValueError):
                    continue
        orchestration_interface_limits: dict[str, int] = {}
        raw_orchestration_limits = raw.get("orchestration_interface_limits")
        if isinstance(raw_orchestration_limits, dict):
            for key, count in list(raw_orchestration_limits.items())[:MAX_CONTRACT_ITEMS]:
                clean_key = _safe_text(key, 120).strip()
                if not clean_key:
                    continue
                try:
                    orchestration_interface_limits[clean_key] = max(0, min(64, int(count)))
                except (TypeError, ValueError):
                    continue
        requirements = [item for item in (_sanitize_requirement(row) for row in (raw.get("pass_requirements") or [])[:MAX_CONTRACT_ITEMS]) if item]
        prior_attempt_requirements = [
            item for item in (
                _sanitize_requirement(row) for row in (raw.get("prior_attempt_requirements") or [])[:MAX_CONTRACT_ITEMS]
            ) if item
        ]
        evidence_scope = str(raw.get("evidence_scope") or "run_scope").strip().lower()
        if evidence_scope not in {"run_scope", "current_acceptance_run"}:
            evidence_scope = "run_scope"
        try:
            min_evidence_refs = max(0, min(MAX_EVIDENCE_REFS, int(raw.get("min_evidence_refs") or 0)))
        except (TypeError, ValueError):
            min_evidence_refs = 0
        try:
            min_candidate_refs = max(0, min(32, int(raw.get("min_candidate_refs") or 0)))
        except (TypeError, ValueError):
            min_candidate_refs = 0
        plan.append({
            "test_id": test_id,
            "objective": objective,
            "allowed_actions": allowed,
            "forbidden_actions": forbidden,
            "required_interfaces": required_interfaces,
            "required_orchestration_interfaces": required_orchestration_interfaces,
            "required_observations": required_observations,
            "pass_requirements": requirements,
            "prior_attempt_requirements": prior_attempt_requirements,
            "evidence_scope": evidence_scope,
            "min_evidence_refs": min_evidence_refs,
            "min_candidate_refs": min_candidate_refs,
            "native_tool_policy": "restricted" if native_policy_declared else "unspecified",
            "allowed_native_tools": allowed_native_tools,
            "native_tool_limits": limits,
            "interface_limits": interface_limits,
            "orchestration_interface_limits": orchestration_interface_limits,
            "forbidden_tool_classes": forbidden_tool_classes,
            "stop_after": bool(raw.get("stop_after")),
        })
    return plan


def start(
    root: Path,
    project_id: str,
    *,
    suite: str,
    title: str = "",
    repo: str = "",
    source_id: str = "",
    expected_tests: list[str] | None = None,
    expected_invariants: list[str] | None = None,
    test_plan: list[dict[str, Any]] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    scope = _scope_snapshot(root, project_id, repo=repo, source_id=source_id)
    if scope.get("status") != "ok":
        return scope
    run_id = "acr_" + uuid.uuid4().hex[:16]
    plan = _sanitize_test_plan(test_plan)
    tests = [_clean_id(item) for item in (expected_tests or [])[:MAX_TESTS] if str(item).strip()]
    if not tests and plan:
        tests = [row["test_id"] for row in plan]
    plan = [row for row in plan if row["test_id"] in set(tests)] if tests else plan
    invariants = [_clean_id(item) for item in (expected_invariants or [])[:MAX_TESTS] if str(item).strip()]
    work_state = work_ledger.status(root, session_id) if session_id else {}
    start_compaction_generation = int(work_state.get("compaction_generation") or 0) if isinstance(work_state, dict) else 0
    state = {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": "running",
        "suite": str(suite or "acceptance")[:240],
        "title": str(title or suite or "Acceptance run")[:400],
        "project_id": project_id,
        "scope": scope,
        "expected_tests": tests,
        "expected_invariants": invariants,
        "test_plan": plan,
        "records": {},
        "attempt_history": {},
        "invariants": {},
        "tool_provenance": {},  # backward-compatible alias for execution_provenance
        "execution_provenance": {},
        "orchestration_provenance": {},
        "contract_enforcement": "machine",
        "compaction_generation_at_start": start_compaction_generation,
        "compaction_generation": start_compaction_generation,
        "compaction_count": 0,
        "last_compacted_at": "",
        "compaction_events": [],
        "created_at": _now(),
        "updated_at": _now(),
        "completed_at": "",
        "origin_session_key": _session_key(session_id) if session_id else "",
        "assessment_basis": "structured observations plus machine-observed protocol/evidence provenance; persistence prevents compaction loss but does not convert model-recorded evidence or model inference into machine proof",
    }
    path = _path(root, project_id, run_id)
    _write(path, state)
    if session_id:
        _write(_session_path(root, session_id), {"run_id": run_id, "project_id": project_id, "updated_at": _now()})
    return {
        "status": "started", "run_id": run_id, "project_id": project_id, "scope": scope,
        "expected_tests": tests, "expected_invariants": invariants, "test_plan": plan,
        "compaction_generation": start_compaction_generation, "compaction_count": 0,
    }


def _locate(root: Path, run_id: str, *, project_id: str = "", session_id: str = "") -> tuple[Path | None, str]:
    rid = _clean_id(run_id) if run_id else ""
    pid = project_id
    if (not rid or not pid) and session_id:
        active = _read(_session_path(root, session_id))
        rid = rid or str(active.get("run_id") or "")
        pid = pid or str(active.get("project_id") or "")
    if not rid or not pid:
        return None, pid
    return _path(root, pid, rid), pid


def _legacy_evidence_artifact(
    root: Path, project_id: str, state: dict[str, Any], *, record_type: str, record_id: str, evidence: Any
) -> dict[str, Any] | None:
    if evidence in (None, {}, []):
        return None
    stored = evidence_store.put(
        root,
        project_id,
        kind="legacy_acceptance_observation",
        tool="acceptance_run_v1_migration",
        payload={"record_type": record_type, "record_id": record_id, "legacy_evidence": evidence},
        scope_identity=_scope_identity(dict(state.get("scope") or {})),
    )
    if stored.get("status") != "stored":
        raise LegacyAcceptanceMigrationError(
            f"could not preserve v1 evidence outside compact ledger: {stored.get('reason') or stored.get('status') or 'unknown'}"
        )
    return {
        "evidence_ref": stored.get("evidence_ref"),
        "kind": stored.get("kind"),
        "tool": stored.get("tool"),
        "artifact_sha256": stored.get("artifact_sha256"),
        "payload_sha256": stored.get("payload_sha256"),
        "authority": "legacy_model_record_only",
    }


def _migrate_legacy_state(root: Path, project_id: str, state: dict[str, Any]) -> bool:
    """Move v1 free-form evidence out of the compact ledger without discarding it."""
    if state.get("schema") != LEGACY_SCHEMA:
        return False
    warnings: list[str] = list(state.get("migration_warnings") or [])
    records = dict(state.get("records") or {})
    for test_id, raw_row in records.items():
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        legacy_evidence = row.get("evidence") if "evidence" in row else {}
        compact, errors = _sanitize_observations(legacy_evidence if isinstance(legacy_evidence, dict) else {})
        legacy_ref = _legacy_evidence_artifact(
            root, project_id, state, record_type="test", record_id=str(test_id), evidence=legacy_evidence
        )
        row["evidence"] = compact
        row["observations"] = compact
        row.setdefault("evidence_refs", [])
        row.setdefault("candidates", [])
        row.setdefault("primary_candidate_id", "")
        if legacy_ref:
            row["legacy_evidence_refs"] = [legacy_ref]
        row["authority"] = "legacy_recorded_observation"
        if errors:
            row["migration_warning"] = "v1 free-form evidence was moved to a non-RAG legacy evidence artifact; rejected v2 fields were removed from the compact ledger"
            warnings.append(f"test:{test_id}:" + "; ".join(errors[:8]))
        records[test_id] = row
    invariants = dict(state.get("invariants") or {})
    for invariant_id, raw_row in invariants.items():
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        legacy_evidence = row.get("evidence") if "evidence" in row else {}
        compact, errors = _sanitize_observations(legacy_evidence if isinstance(legacy_evidence, dict) else {})
        legacy_ref = _legacy_evidence_artifact(
            root, project_id, state, record_type="invariant", record_id=str(invariant_id), evidence=legacy_evidence
        )
        row["evidence"] = compact
        row["observations"] = compact
        row.setdefault("evidence_refs", [])
        if legacy_ref:
            row["legacy_evidence_refs"] = [legacy_ref]
        if errors:
            row["migration_warning"] = "v1 free-form evidence was moved to a non-RAG legacy evidence artifact; rejected v2 fields were removed from the compact ledger"
            warnings.append(f"invariant:{invariant_id}:" + "; ".join(errors[:8]))
        invariants[invariant_id] = row
    state["schema"] = SCHEMA
    state["records"] = records
    state["invariants"] = invariants
    state["migration_warnings"] = warnings[:64]
    state["migrated_from_schema"] = LEGACY_SCHEMA
    state["updated_at"] = _now()
    return True


def _attempt_id(run_id: str, test_id: str, number: int, *, nonce: str = "") -> str:
    seed = f"{run_id}|{test_id}|{number}|{nonce}".encode("utf-8")
    return "aat_" + hashlib.sha256(seed).hexdigest()[:20]


def _ensure_attempt_history(state: dict[str, Any]) -> bool:
    """Upgrade old latest-only records into one immutable synthetic attempt each."""
    changed = False
    history = dict(state.get("attempt_history") or {})
    records = dict(state.get("records") or {})
    run_id = str(state.get("run_id") or "")
    for test_id, raw in records.items():
        if not isinstance(raw, dict):
            continue
        existing = [dict(row) for row in (history.get(test_id) or []) if isinstance(row, dict)]
        if existing:
            continue
        row = dict(raw)
        row.setdefault("attempt_number", 1)
        row.setdefault("attempt_id", _attempt_id(run_id, str(test_id), 1, nonce=str(row.get("recorded_at") or "legacy")))
        row.setdefault("supersedes_attempt_id", "")
        history[str(test_id)] = [row]
        records[str(test_id)] = row
        changed = True
    if state.get("attempt_history") != history:
        state["attempt_history"] = history
        changed = True
    if state.get("records") != records:
        state["records"] = records
        changed = True
    return changed

def _upgrade_state(root: Path, project_id: str, state: dict[str, Any]) -> bool:
    changed = False
    original_schema = str(state.get("schema") or "")
    if original_schema == LEGACY_SCHEMA:
        changed = _migrate_legacy_state(root, project_id, state) or changed
        original_schema = LEGACY_SCHEMA
    elif original_schema in {PREVIOUS_SCHEMA, V3_SCHEMA}:
        state["schema"] = SCHEMA
        state["test_plan"] = _sanitize_test_plan(state.get("test_plan") if isinstance(state.get("test_plan"), list) else [])
        state.setdefault("tool_provenance", {})
        state.setdefault("execution_provenance", dict(state.get("tool_provenance") or {}))
        state.setdefault("orchestration_provenance", {})
        state.setdefault("compaction_generation_at_start", 0)
        state.setdefault("compaction_generation", int(state.get("compaction_generation_at_start") or 0))
        state.setdefault("compaction_count", 0)
        state.setdefault("last_compacted_at", "")
        state.setdefault("compaction_events", [])
        state.setdefault("attempt_history", {})
        state["contract_enforcement"] = str(state.get("contract_enforcement") or "legacy_best_effort")
        state["migrated_from_schema"] = original_schema
        state["updated_at"] = _now()
        changed = True
    if state.get("schema") == SCHEMA:
        normalized_plan = _sanitize_test_plan(state.get("test_plan") if isinstance(state.get("test_plan"), list) else [])
        if normalized_plan != state.get("test_plan"):
            state["test_plan"] = normalized_plan
            changed = True
        state.setdefault("tool_provenance", {})
        state.setdefault("execution_provenance", dict(state.get("tool_provenance") or {}))
        state.setdefault("orchestration_provenance", {})
        state.setdefault("compaction_generation_at_start", 0)
        state.setdefault("compaction_generation", int(state.get("compaction_generation_at_start") or 0))
        state.setdefault("compaction_count", 0)
        state.setdefault("last_compacted_at", "")
        state.setdefault("compaction_events", [])
        state.setdefault("attempt_history", {})
        changed = _ensure_attempt_history(state) or changed
    return changed

def status(root: Path, *, run_id: str = "", project_id: str = "", session_id: str = "") -> dict[str, Any]:
    path, pid = _locate(root, run_id, project_id=project_id, session_id=session_id)
    if path is None:
        return {"status": "none", "run_id": run_id, "project_id": pid}
    with _lock(path):
        state = _read(path)
        if not state:
            return {"status": "none", "run_id": run_id, "project_id": pid}
        try:
            migrated = _upgrade_state(root, pid, state)
        except LegacyAcceptanceMigrationError as exc:
            return {
                "status": "migration_blocked", "reason": "legacy_acceptance_migration_failed",
                "run_id": run_id, "project_id": pid, "detail": str(exc)[:400],
            }
        if migrated:
            _write(path, state)
    run_status = str(state.get("status") or "")
    payload = dict(state)
    payload["run_status"] = run_status
    payload["finalized"] = run_status == "completed"
    payload["status"] = "ok"
    return payload


def next_step(root: Path, *, run_id: str = "", project_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Return the next unfinished acceptance step without inventing analytical content.

    This is orchestration metadata only: the acceptance ledger does not own the
    reliability corrective budget, so callers must query reliability_status when
    that separate epistemic budget is relevant.
    """
    current = status(root, run_id=run_id, project_id=project_id, session_id=session_id)
    if current.get("status") != "ok":
        return current
    expected = [str(item) for item in current.get("expected_tests") or []]
    records = dict(current.get("records") or {})
    remaining = [item for item in expected if item not in records]
    refs: list[str] = []
    for row in records.values():
        if not isinstance(row, dict):
            continue
        for ref in row.get("evidence_refs") or []:
            if isinstance(ref, dict) and ref.get("evidence_ref"):
                value = str(ref["evidence_ref"])
                if value not in refs:
                    refs.append(value)
    if current.get("run_status") == "completed" or not remaining:
        return {
            "status": "complete" if current.get("run_status") == "completed" else "awaiting_finalize",
            "run_id": current.get("run_id"),
            "completed_tests": list(records),
            "remaining_tests": remaining,
            "evidence_refs": refs[:32],
            "corrective_budget_state": "owned by reliability ledger; query reliability_status",
        }
    test_id = remaining[0]
    plan_map = {str(row.get("test_id") or ""): row for row in current.get("test_plan") or [] if isinstance(row, dict)}
    spec = dict(plan_map.get(test_id) or {
        "test_id": test_id, "objective": "", "allowed_actions": [], "forbidden_actions": [],
        "required_interfaces": [], "required_orchestration_interfaces": [], "required_observations": [], "pass_requirements": [],
        "prior_attempt_requirements": [],
        "evidence_scope": "run_scope", "min_evidence_refs": 0, "min_candidate_refs": 0,
        "native_tool_policy": "unspecified", "allowed_native_tools": [], "native_tool_limits": {},
        "interface_limits": {}, "orchestration_interface_limits": {},
        "forbidden_tool_classes": [], "stop_after": False,
    })
    execution_provenance = dict((current.get("execution_provenance") or current.get("tool_provenance") or {}).get(test_id) or {})
    orchestration_provenance = dict((current.get("orchestration_provenance") or {}).get(test_id) or {})
    return {
        "status": "ready",
        "run_id": current.get("run_id"),
        "test_id": test_id,
        "objective": spec.get("objective") or "",
        "allowed_actions": list(spec.get("allowed_actions") or []),
        "forbidden_actions": list(spec.get("forbidden_actions") or []),
        "required_interfaces": list(spec.get("required_interfaces") or []),
        "required_orchestration_interfaces": list(spec.get("required_orchestration_interfaces") or []),
        "required_observations": list(spec.get("required_observations") or []),
        "pass_requirements": list(spec.get("pass_requirements") or []),
        "prior_attempt_requirements": list(spec.get("prior_attempt_requirements") or []),
        "evidence_scope": str(spec.get("evidence_scope") or "run_scope"),
        "min_evidence_refs": int(spec.get("min_evidence_refs") or 0),
        "min_candidate_refs": int(spec.get("min_candidate_refs") or 0),
        "native_tool_policy": str(spec.get("native_tool_policy") or "unspecified"),
        "allowed_native_tools": list(spec.get("allowed_native_tools") or []),
        "native_tool_limits": dict(spec.get("native_tool_limits") or {}),
        "interface_limits": dict(spec.get("interface_limits") or {}),
        "orchestration_interface_limits": dict(spec.get("orchestration_interface_limits") or {}),
        "forbidden_tool_classes": list(spec.get("forbidden_tool_classes") or []),
        "observed_tool_provenance": execution_provenance,
        "observed_execution_provenance": execution_provenance,
        "observed_orchestration_provenance": orchestration_provenance,
        "stop_after": bool(spec.get("stop_after")),
        "completed_tests": list(records),
        "current_test_attempts": list((current.get("attempt_history") or {}).get(test_id) or []),
        "remaining_tests": remaining,
        "evidence_refs": refs[:32],
        "scope": current.get("scope"),
        "compaction_generation": int(current.get("compaction_generation") or 0),
        "compaction_count": int(current.get("compaction_count") or 0),
        "corrective_budget_state": "owned by reliability ledger; query reliability_status",
        "guidance": "Execute only this acceptance step, record it, then call acceptance_run_next again. Analytical reasoning remains free-form outside this orchestration record.",
        "policy_enforcement": "allowed_actions/forbidden_actions are bounded suite labels, not authorization grants; underlying Awoki/tool policy remains authoritative",
    }


def _resolve_evidence_refs(
    root: Path, project_id: str, state: dict[str, Any], refs: list[str] | None
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    resolved: list[dict[str, Any]] = []
    candidates: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    expected_identity = _scope_identity(dict(state.get("scope") or {}))
    for ref in (refs or [])[:MAX_EVIDENCE_REFS]:
        evidence_ref = str(ref or "").strip()
        if not evidence_ref:
            continue
        meta = evidence_store.metadata(root, project_id, evidence_ref)
        if meta.get("status") != "ok":
            errors.append(f"evidence_ref not found: {evidence_ref}")
            continue
        if dict(meta.get("scope_identity") or {}) != expected_identity:
            errors.append(f"evidence_ref scope/revision mismatch: {evidence_ref}")
            continue
        resolved.append({
            "evidence_ref": evidence_ref,
            "kind": meta.get("kind"),
            "tool": meta.get("tool"),
            "payload_sha256": meta.get("payload_sha256"),
            "candidate_count": meta.get("candidate_count"),
            "capture_run_ids": list(meta.get("capture_run_ids") or []),
        })
        for row in meta.get("candidate_index") or []:
            if isinstance(row, dict) and row.get("candidate_id"):
                candidates[str(row["candidate_id"])] = dict(row)
    return resolved, candidates, errors


def record_tool_event(
    root: Path,
    session_id: str,
    *,
    tool: str,
    tool_class: str,
    phase: str,
) -> dict[str, Any]:
    """Attach bounded structural provenance to the current unfinished test.

    Execution and acceptance orchestration are separate provenance domains. This
    avoids circular self-proof while allowing a test to prove that recovery used
    acceptance_run_status/acceptance_run_next. Arguments, outputs, source content,
    and model reasoning are never recorded here.
    """
    active = _read(_session_path(root, session_id)) if session_id else {}
    run_id = str(active.get("run_id") or "")
    project_id = str(active.get("project_id") or "")
    clean_tool = _safe_text(tool, 160).strip()
    clean_class = _safe_text(tool_class, 80).strip().lower() or "other"
    clean_phase = str(phase or "").strip().lower()
    if not run_id or not project_id or not clean_tool or clean_phase not in {"started", "completed"}:
        return {"status": "ignored", "reason": "no_active_acceptance_or_invalid_event"}
    if clean_tool in _CONTROL_ORCHESTRATION_TOOLS:
        return {"status": "ignored", "reason": "acceptance_control_tool_not_self_proving"}
    domain = "orchestration" if clean_tool in _OBSERVABLE_ORCHESTRATION_TOOLS else "execution"
    path = _path(root, project_id, run_id)
    with _lock(path):
        state = _read(path)
        if not state or state.get("status") != "running":
            return {"status": "ignored", "reason": "acceptance_run_not_running"}
        try:
            _upgrade_state(root, project_id, state)
        except LegacyAcceptanceMigrationError:
            return {"status": "ignored", "reason": "acceptance_migration_blocked"}
        expected = [str(item) for item in state.get("expected_tests") or []]
        records = dict(state.get("records") or {})
        pending = [item for item in expected if item not in records]
        if not pending:
            return {"status": "ignored", "reason": "no_pending_test"}
        test_id = pending[0]
        key = "orchestration_provenance" if domain == "orchestration" else "execution_provenance"
        all_provenance = dict(state.get(key) or {})
        test_provenance = dict(all_provenance.get(test_id) or {})
        events = [dict(row) for row in (test_provenance.get("events") or []) if isinstance(row, dict)]
        events.append({"tool": clean_tool, "tool_class": clean_class, "phase": clean_phase, "observed_at": _now()})
        events = events[-MAX_TOOL_EVENTS_PER_TEST:]
        invocations = dict(test_provenance.get("invocations") or {})
        entry = dict(invocations.get(clean_tool) or {"tool_class": clean_class, "started": 0, "completed": 0})
        entry["tool_class"] = clean_class
        entry[clean_phase] = int(entry.get(clean_phase) or 0) + 1
        invocations[clean_tool] = entry
        test_provenance = {"events": events, "invocations": invocations, "updated_at": _now()}
        all_provenance[test_id] = test_provenance
        state[key] = all_provenance
        if domain == "execution":
            state["tool_provenance"] = all_provenance
        state["updated_at"] = _now()
        _write(path, state)
    return {
        "status": "recorded", "run_id": run_id, "test_id": test_id,
        "tool": clean_tool, "phase": clean_phase, "provenance_domain": domain,
    }


def mark_compacted(
    root: Path, session_id: str, *, generation: int, trigger: str = "unknown", trigger_source: str = ""
) -> dict[str, Any]:
    active = _read(_session_path(root, session_id)) if session_id else {}
    run_id = str(active.get("run_id") or "")
    project_id = str(active.get("project_id") or "")
    if not run_id or not project_id:
        return {"status": "none"}
    path = _path(root, project_id, run_id)
    with _lock(path):
        state = _read(path)
        if not state or state.get("status") != "running":
            return {"status": "none"}
        try:
            _upgrade_state(root, project_id, state)
        except LegacyAcceptanceMigrationError:
            return {"status": "migration_blocked"}
        state["compaction_generation"] = max(int(state.get("compaction_generation") or 0), int(generation or 0))
        state["compaction_count"] = int(state.get("compaction_count") or 0) + 1
        observed_at = _now()
        state["last_compacted_at"] = observed_at
        events = [dict(row) for row in (state.get("compaction_events") or []) if isinstance(row, dict)]
        clean_trigger = str(trigger or "unknown").strip().lower()
        if clean_trigger not in {"automatic_context_pressure", "explicit_request", "unknown"}:
            clean_trigger = "unknown"
        events.append({
            "generation": int(state["compaction_generation"]),
            "count_since_run_start": int(state["compaction_count"]),
            "observed_at": observed_at,
            "trigger": clean_trigger,
            "trigger_source": _safe_text(trigger_source, 120),
        })
        state["compaction_events"] = events[-MAX_COMPACTION_EVENTS:]
        state["updated_at"] = observed_at
        _write(path, state)
    return {
        "status": "marked", "run_id": run_id,
        "compaction_generation": state["compaction_generation"], "compaction_count": state["compaction_count"],
        "compaction_trigger": clean_trigger,
        "compaction_events": list(state.get("compaction_events") or []),
    }


def _resolve_candidates(
    candidate_map: dict[str, dict[str, Any]], candidate_ids: list[str] | None, primary_candidate_id: str
) -> tuple[list[dict[str, Any]], str, list[str]]:
    requested = [str(item or "").strip() for item in (candidate_ids or [])[:32] if str(item or "").strip()]
    primary = str(primary_candidate_id or "").strip()
    if primary and primary not in requested:
        requested.insert(0, primary)
    resolved: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for candidate_id in requested:
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        row = candidate_map.get(candidate_id)
        if row is None:
            errors.append(f"candidate_id not found in referenced evidence: {candidate_id}")
        else:
            resolved.append(dict(row))
    if primary and primary not in {str(row.get("candidate_id") or "") for row in resolved}:
        errors.append(f"primary_candidate_id not found in referenced evidence: {primary}")
    if not primary and len(resolved) == 1:
        primary = str(resolved[0].get("candidate_id") or "")
    return resolved, primary, errors


def _lookup_field(value: dict[str, Any], field: str) -> tuple[bool, Any]:
    current: Any = value
    for part in [item for item in str(field or "").split(".") if item]:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _requirement_met(observations: dict[str, Any], requirement: dict[str, Any]) -> bool:
    exists, actual = _lookup_field(observations, str(requirement.get("field") or ""))
    op = str(requirement.get("op") or "eq")
    expected = requirement.get("value")
    if op == "exists":
        return exists
    if op == "not_exists":
        return not exists
    if not exists:
        return False
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not bool(actual)
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op == "in":
        return isinstance(expected, list) and actual in expected
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError):
        return False
    if op == "gte":
        return left >= right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "lt":
        return left < right
    return False


def _prior_attempt_context(prior_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    last = dict(prior_attempts[-1]) if prior_attempts else {}
    return {
        "count": len(prior_attempts),
        "exists": bool(prior_attempts),
        "last": {
            "attempt_id": str(last.get("attempt_id") or ""),
            "attempt_number": int(last.get("attempt_number") or 0),
            "claimed_outcome": str(last.get("claimed_outcome") or ""),
            "effective_outcome": str(last.get("outcome") or ""),
            "supersedes_attempt_id": str(last.get("supersedes_attempt_id") or ""),
        },
    }


def _evaluate_test_contract(
    state: dict[str, Any],
    test_id: str,
    *,
    claimed_outcome: str,
    observations: dict[str, Any],
    resolved_refs: list[dict[str, Any]],
    resolved_candidates: list[dict[str, Any]],
    prior_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plan_map = {str(row.get("test_id") or ""): row for row in state.get("test_plan") or [] if isinstance(row, dict)}
    spec = dict(plan_map.get(test_id) or {})
    execution_provenance = dict((state.get("execution_provenance") or state.get("tool_provenance") or {}).get(test_id) or {})
    orchestration_provenance = dict((state.get("orchestration_provenance") or {}).get(test_id) or {})
    invocations = dict(execution_provenance.get("invocations") or {})
    orchestration_invocations = dict(orchestration_provenance.get("invocations") or {})
    completed_tools = {
        str(tool): int((entry or {}).get("completed") or 0)
        for tool, entry in invocations.items() if isinstance(entry, dict)
    }
    completed_orchestration_tools = {
        str(tool): int((entry or {}).get("completed") or 0)
        for tool, entry in orchestration_invocations.items() if isinstance(entry, dict)
    }
    protocol_violations: list[str] = []
    incomplete_reasons: list[str] = []
    prior_attempt_context = _prior_attempt_context([dict(row) for row in (prior_attempts or []) if isinstance(row, dict)])

    for tool in spec.get("required_interfaces") or []:
        if completed_tools.get(str(tool), 0) <= 0:
            incomplete_reasons.append(f"required_interface_not_observed:{tool}")
    for tool in spec.get("required_orchestration_interfaces") or []:
        if completed_orchestration_tools.get(str(tool), 0) <= 0:
            incomplete_reasons.append(f"required_orchestration_interface_not_observed:{tool}")

    for tool, limit in dict(spec.get("interface_limits") or {}).items():
        observed = int((invocations.get(str(tool)) or {}).get("started") or 0)
        if observed > int(limit):
            protocol_violations.append(f"interface_limit_exceeded:{tool}:{observed}>{int(limit)}")
    for tool, limit in dict(spec.get("orchestration_interface_limits") or {}).items():
        observed = int((orchestration_invocations.get(str(tool)) or {}).get("started") or 0)
        if observed > int(limit):
            protocol_violations.append(f"orchestration_interface_limit_exceeded:{tool}:{observed}>{int(limit)}")

    forbidden_classes = {str(item).strip().lower() for item in spec.get("forbidden_tool_classes") or []}
    for tool, entry in invocations.items():
        if not isinstance(entry, dict) or int(entry.get("started") or 0) <= 0:
            continue
        tool_class = str(entry.get("tool_class") or "other").strip().lower()
        if tool_class in forbidden_classes:
            protocol_violations.append(f"forbidden_tool_class:{tool_class}:{tool}")

    if str(spec.get("native_tool_policy") or "unspecified") == "restricted":
        allowed_native = {str(item) for item in spec.get("allowed_native_tools") or []}
        limits = {str(key): int(value or 0) for key, value in dict(spec.get("native_tool_limits") or {}).items()}
        for tool, entry in invocations.items():
            if not isinstance(entry, dict) or str(entry.get("tool_class") or "").lower() != "native":
                continue
            started = int(entry.get("started") or 0)
            if started <= 0:
                continue
            if tool not in allowed_native:
                protocol_violations.append(f"native_tool_not_allowed:{tool}")
            if tool in limits and started > limits[tool]:
                protocol_violations.append(f"native_tool_limit_exceeded:{tool}:{started}>{limits[tool]}")

    for field in spec.get("required_observations") or []:
        exists, _ = _lookup_field(observations, str(field))
        if not exists:
            incomplete_reasons.append(f"required_observation_missing:{field}")
    for requirement in spec.get("pass_requirements") or []:
        if isinstance(requirement, dict) and not _requirement_met(observations, requirement):
            incomplete_reasons.append(
                f"pass_requirement_unmet:{requirement.get('field')}:{requirement.get('op')}:{requirement.get('value')}"
            )
    for requirement in spec.get("prior_attempt_requirements") or []:
        if isinstance(requirement, dict) and not _requirement_met(prior_attempt_context, requirement):
            incomplete_reasons.append(
                f"prior_attempt_requirement_unmet:{requirement.get('field')}:{requirement.get('op')}:{requirement.get('value')}"
            )

    min_refs = int(spec.get("min_evidence_refs") or 0)
    if len(resolved_refs) < min_refs:
        incomplete_reasons.append(f"evidence_refs_below_minimum:{len(resolved_refs)}<{min_refs}")
    min_candidates = int(spec.get("min_candidate_refs") or 0)
    if len(resolved_candidates) < min_candidates:
        incomplete_reasons.append(f"candidate_refs_below_minimum:{len(resolved_candidates)}<{min_candidates}")
    if str(spec.get("evidence_scope") or "run_scope") == "current_acceptance_run":
        for ref in resolved_refs:
            if str(state.get("run_id") or "") not in {str(item) for item in ref.get("capture_run_ids") or []}:
                protocol_violations.append(f"evidence_not_captured_in_current_run:{ref.get('evidence_ref')}")

    effective_outcome = claimed_outcome
    machine_adjustment = ""
    if claimed_outcome == "pass" and protocol_violations:
        effective_outcome = "protocol_deviation"
        machine_adjustment = "claimed pass downgraded because machine-observed tool/evidence protocol violated the durable test contract"
    elif claimed_outcome == "pass" and incomplete_reasons:
        effective_outcome = "incomplete"
        machine_adjustment = "claimed pass downgraded because durable pass requirements were not satisfied"
    return {
        "contract_present": bool(spec),
        "claimed_outcome": claimed_outcome,
        "effective_outcome": effective_outcome,
        "protocol_violations": protocol_violations[:64],
        "incomplete_reasons": incomplete_reasons[:64],
        "machine_adjustment": machine_adjustment,
        "tool_provenance": {"invocations": invocations},
        "execution_provenance": {"invocations": invocations},
        "orchestration_provenance": {"invocations": orchestration_invocations},
        "prior_attempt_context": prior_attempt_context,
    }


def record(
    root: Path,
    *,
    run_id: str,
    project_id: str,
    test_id: str,
    outcome: str,
    query: str = "",
    targets: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    candidate_ids: list[str] | None = None,
    primary_candidate_id: str = "",
    notes: str = "",
    violations: list[str] | None = None,
) -> dict[str, Any]:
    path = _path(root, project_id, run_id)
    test_key = _clean_id(test_id)
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in _OUTCOMES:
        return {"status": "rejected", "reason": "unsupported acceptance outcome", "allowed_outcomes": sorted(_OUTCOMES)}
    with _lock(path):
        state = _read(path)
        try:
            if state:
                _upgrade_state(root, project_id, state)
        except LegacyAcceptanceMigrationError as exc:
            return {"status": "rejected", "reason": "legacy_acceptance_migration_failed", "detail": str(exc)[:400]}
        if not state or state.get("schema") not in SUPPORTED_SCHEMAS:
            return {"status": "none", "run_id": run_id, "project_id": project_id}
        if state.get("status") != "running":
            return {"status": "rejected", "reason": "acceptance run is not running", "run_status": state.get("status")}
        scope_guard = _scope_guard(root, state)
        if scope_guard.get("status") != "ok":
            return {"status": "rejected", "reason": "acceptance_scope_drift", "scope_guard": scope_guard}
        resolved_refs, candidate_map, ref_errors = _resolve_evidence_refs(root, project_id, state, evidence_refs)
        resolved_candidates, primary, candidate_errors = _resolve_candidates(candidate_map, candidate_ids, primary_candidate_id)
        clean_observations, observation_errors = _sanitize_observations(
            evidence, forbid_candidate_metrics=bool(resolved_refs or candidate_ids or primary_candidate_id or len(targets or []) > 1)
        )
        errors = ref_errors + candidate_errors + observation_errors
        try:
            clean_notes = _safe_compact_text(notes)
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            return {
                "status": "rejected",
                "reason": "acceptance_evidence_invalid",
                "errors": errors,
                "guidance": (
                    f"Keep notes/evidence compact (notes <= {MAX_OBSERVATION_STRING} characters and <= 4 newlines). "
                    "Put machine-checkable facts in evidence=/observations, rich Awoki output in ev_ evidence via capture_evidence=true, "
                    "and pass evidence_refs plus canonical candidate_ids. Use durable reference labels/why_saved for human navigation instead of copying long context into notes."
                ),
            }
        records = dict(state.get("records") or {})
        history = dict(state.get("attempt_history") or {})
        prior_attempts = [dict(row) for row in (history.get(test_key) or []) if isinstance(row, dict)]
        contract = _evaluate_test_contract(
            state, test_key, claimed_outcome=clean_outcome, observations=clean_observations,
            resolved_refs=resolved_refs, resolved_candidates=resolved_candidates, prior_attempts=prior_attempts,
        )
        effective_outcome = str(contract.get("effective_outcome") or clean_outcome)
        if len(prior_attempts) >= MAX_ATTEMPTS_PER_TEST:
            return {
                "status": "rejected", "reason": "acceptance_attempt_limit_reached",
                "test_id": test_key, "max_attempts": MAX_ATTEMPTS_PER_TEST,
                "guidance": "Do not loop re-recording a test. Preserve the existing machine outcome and investigate explicitly before another acceptance run.",
            }
        attempt_number = len(prior_attempts) + 1
        prior_attempt_id = str(prior_attempts[-1].get("attempt_id") or "") if prior_attempts else ""
        recorded_at = _now()
        attempt = {
            "attempt_id": _attempt_id(str(state.get("run_id") or run_id), test_key, attempt_number, nonce=recorded_at),
            "attempt_number": attempt_number,
            "supersedes_attempt_id": prior_attempt_id,
            "test_id": test_key,
            "outcome": effective_outcome,
            "claimed_outcome": clean_outcome,
            "query": _safe_text(query),
            "targets": [_safe_text(item, 400) for item in (targets or [])[:32]],
            "evidence": clean_observations,
            "observations": clean_observations,
            "evidence_refs": resolved_refs,
            "candidates": resolved_candidates,
            "primary_candidate_id": primary,
            "notes": clean_notes,
            "violations": [_clean_id(item) for item in (violations or [])[:32] if str(item).strip()],
            "protocol_evaluation": contract,
            "recorded_at": recorded_at,
            "authority": "evidence_backed_observation" if resolved_refs else "recorded_observation",
            "correction_scope": "acceptance_record_only; does not consume reliability corrective_budget",
        }
        prior_attempts.append(attempt)
        history[test_key] = prior_attempts
        records[test_key] = attempt
        state["schema"] = SCHEMA
        state["attempt_history"] = history
        state["records"] = records
        state["updated_at"] = _now()
        _write(path, state)
    prior_attempt = prior_attempts[-2] if len(prior_attempts) > 1 else {}
    prior_attempt_count = max(0, len(prior_attempts) - 1)
    return {
        "status": "recorded", "run_id": run_id, "test_id": test_key, "outcome": effective_outcome,
        "claimed_outcome": clean_outcome,
        "machine_adjustment": contract.get("machine_adjustment") or "",
        "protocol_violations": list(contract.get("protocol_violations") or []),
        "incomplete_reasons": list(contract.get("incomplete_reasons") or []),
        "record_count": len(records), "evidence_refs": [row["evidence_ref"] for row in resolved_refs],
        "candidate_ids": [row.get("candidate_id") for row in resolved_candidates], "primary_candidate_id": primary,
        "attempt_id": attempt["attempt_id"], "attempt_number": attempt_number,
        "supersedes_attempt_id": prior_attempt_id,
        "attempt_history_count": len(prior_attempts),
        "prior_attempt_count": prior_attempt_count,
        "prior_attempt_id": str(prior_attempt.get("attempt_id") or ""),
        "prior_attempt_effective_outcome": str(prior_attempt.get("outcome") or ""),
        "prior_attempt_claimed_outcome": str(prior_attempt.get("claimed_outcome") or ""),
        "attempt_summary": {
            "attempt_id": attempt["attempt_id"],
            "attempt_number": attempt_number,
            "claimed_outcome": clean_outcome,
            "effective_outcome": effective_outcome,
            "supersedes_attempt_id": prior_attempt_id,
            "prior_attempt_count": prior_attempt_count,
            "prior_attempt_id": str(prior_attempt.get("attempt_id") or ""),
            "prior_attempt_effective_outcome": str(prior_attempt.get("outcome") or ""),
        },
    }


def attempt_by_id(root: Path, project_id: str, attempt_id: str) -> dict[str, Any]:
    """Resolve a stable acceptance-attempt ID without changing current test outcome."""
    clean = str(attempt_id or "").strip()
    if not clean.startswith("aat_"):
        return {"status": "rejected", "reason": "invalid_attempt_id", "attempt_id": clean}
    base = _dir(root, project_id)
    if not base.is_dir():
        return {"status": "not_found", "attempt_id": clean, "project_id": project_id}
    for path in sorted(base.glob("acr_*.json"), reverse=True):
        state = _read(path)
        if not state:
            continue
        try:
            _upgrade_state(root, project_id, state)
        except LegacyAcceptanceMigrationError:
            continue
        for test_id, rows in dict(state.get("attempt_history") or {}).items():
            for row in rows or []:
                if isinstance(row, dict) and str(row.get("attempt_id") or "") == clean:
                    return {
                        "status": "ok", "attempt_id": clean, "run_id": state.get("run_id"),
                        "test_id": str(test_id), "attempt": dict(row), "scope": dict(state.get("scope") or {}),
                    }
    return {"status": "not_found", "attempt_id": clean, "project_id": project_id}

def record_invariant(
    root: Path,
    *,
    run_id: str,
    project_id: str,
    invariant_id: str,
    outcome: str,
    evidence: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    path = _path(root, project_id, run_id)
    key = _clean_id(invariant_id)
    clean_outcome = str(outcome or "").strip().lower()
    if clean_outcome not in {"hold", "violation", "blocked", "inconclusive"}:
        return {"status": "rejected", "reason": "outcome must be hold|violation|blocked|inconclusive"}
    with _lock(path):
        state = _read(path)
        try:
            if state:
                _upgrade_state(root, project_id, state)
        except LegacyAcceptanceMigrationError as exc:
            return {"status": "rejected", "reason": "legacy_acceptance_migration_failed", "detail": str(exc)[:400]}
        if not state or state.get("status") != "running":
            return {"status": "none" if not state else "rejected", "reason": "acceptance run is not running"}
        scope_guard = _scope_guard(root, state)
        if scope_guard.get("status") != "ok":
            return {"status": "rejected", "reason": "acceptance_scope_drift", "scope_guard": scope_guard}
        resolved_refs, _candidate_map, ref_errors = _resolve_evidence_refs(root, project_id, state, evidence_refs)
        clean_observations, observation_errors = _sanitize_observations(evidence)
        errors = ref_errors + observation_errors
        if errors:
            return {
                "status": "rejected", "reason": "acceptance_evidence_invalid", "errors": errors,
                "guidance": "Store rich tool evidence as an evidence_ref and keep invariant evidence= to small scalar observations.",
            }
        invariants = dict(state.get("invariants") or {})
        invariants[key] = {
            "invariant_id": key, "outcome": clean_outcome, "evidence": clean_observations,
            "observations": clean_observations, "evidence_refs": resolved_refs, "recorded_at": _now(),
        }
        state["schema"] = SCHEMA
        state["invariants"] = invariants
        state["updated_at"] = _now()
        _write(path, state)
    return {
        "status": "recorded", "run_id": run_id, "invariant_id": key, "outcome": clean_outcome,
        "evidence_refs": [row["evidence_ref"] for row in resolved_refs],
    }


def finalize(root: Path, *, run_id: str, project_id: str) -> dict[str, Any]:
    path = _path(root, project_id, run_id)
    with _lock(path):
        state = _read(path)
        try:
            if state:
                _upgrade_state(root, project_id, state)
        except LegacyAcceptanceMigrationError as exc:
            return {"status": "rejected", "reason": "legacy_acceptance_migration_failed", "detail": str(exc)[:400]}
        if not state:
            return {"status": "none", "run_id": run_id, "project_id": project_id}
        if state.get("status") != "running":
            return {
                "status": "rejected", "reason": "acceptance run is not running",
                "run_status": state.get("status"), "run_id": run_id, "project_id": project_id,
            }
        scope_guard = _scope_guard(root, state)
        if scope_guard.get("status") != "ok":
            return {"status": "rejected", "reason": "acceptance_scope_drift", "scope_guard": scope_guard}
        expected = list(state.get("expected_tests") or [])
        expected_invariants = list(state.get("expected_invariants") or [])
        records = dict(state.get("records") or {})
        invariants = dict(state.get("invariants") or {})
        missing = [test_id for test_id in expected if test_id not in records]
        missing_invariants = [key for key in expected_invariants if key not in invariants]
        if missing or missing_invariants:
            # Incomplete is not a failing terminal outcome. Keep the run resumable.
            return {
                "status": "rejected",
                "reason": "acceptance_incomplete",
                "run_id": run_id,
                "project_id": project_id,
                "run_status": "running",
                "finalized": False,
                "missing_tests": missing,
                "missing_invariants": missing_invariants,
                "assessment_basis": state.get("assessment_basis"),
            }
        failing = [key for key, row in records.items() if str(row.get("outcome")) != "pass"]
        invariant_failures = [key for key, row in invariants.items() if str(row.get("outcome")) != "hold"]
        ledger_outcome = "complete" if not failing and not invariant_failures else "not_passed"
        state["schema"] = SCHEMA
        state["status"] = "completed"
        state["ledger_outcome"] = ledger_outcome
        state["missing_tests"] = []
        state["nonpassing_tests"] = failing
        state["missing_invariants"] = []
        state["nonholding_invariants"] = invariant_failures
        state["completed_at"] = _now()
        state["updated_at"] = _now()
        _write(path, state)
    return {
        "status": "finalized",
        "run_id": run_id,
        "project_id": project_id,
        "run_status": "completed",
        "finalized": True,
        "ledger_outcome": ledger_outcome,
        "missing_tests": [],
        "nonpassing_tests": failing,
        "missing_invariants": [],
        "nonholding_invariants": invariant_failures,
        "assessment_basis": state.get("assessment_basis"),
    }


def compact_context(root: Path, session_id: str, *, max_chars: int = 10_000) -> str:
    current = status(root, session_id=session_id)
    if current.get("status") != "ok" or current.get("run_status") == "completed":
        return ""
    records = dict(current.get("records") or {})
    expected = list(current.get("expected_tests") or [])
    expected_invariants = list(current.get("expected_invariants") or [])
    invariants = dict(current.get("invariants") or {})
    pending = [item for item in expected if item not in records]
    pending_invariants = [item for item in expected_invariants if item not in invariants]
    lines = [
        "## Awoki acceptance-run continuity",
        "",
        "This is durable structured acceptance state. Exact prior ranks/scores should be read from acceptance_run_status instead of reconstructed from conversation memory.",
        f"run_id: {current.get('run_id')}",
        f"suite: {current.get('suite')}",
        f"scope: project={current.get('project_id')} repo={((current.get('scope') or {}).get('repo') or '')} source_id={((current.get('scope') or {}).get('source_id') or '')}",
        f"completed tests: {len(records)}/{len(expected) if expected else '?'}",
        "pending tests: " + (", ".join(pending[:32]) if pending else "none"),
        "pending invariants: " + (", ".join(pending_invariants[:32]) if pending_invariants else "none"),
        f"compaction_generation: {int(current.get('compaction_generation') or 0)}",
        f"compaction_count_since_run_start: {int(current.get('compaction_count') or 0)}",
        "compaction_events: " + json.dumps((current.get("compaction_events") or [])[-8:], ensure_ascii=False, sort_keys=True)[:1600],
        "",
        "Recorded outcomes:",
    ]
    for test_id, row in list(records.items())[:MAX_TESTS]:
        refs = [str(ref.get("evidence_ref") or "") for ref in (row.get("evidence_refs") or []) if isinstance(ref, dict)]
        primary = str(row.get("primary_candidate_id") or "")
        suffix = f" primary_candidate={primary}" if primary else ""
        if refs:
            suffix += " evidence_refs=" + ",".join(refs[:4])
        lines.append(f"- {test_id}: {row.get('outcome')} targets={', '.join(row.get('targets') or [])[:500]}{suffix}")
    if pending:
        plan_map = {str(row.get("test_id") or ""): row for row in current.get("test_plan") or [] if isinstance(row, dict)}
        spec = dict(plan_map.get(pending[0]) or {})
        execution_provenance = dict((current.get("execution_provenance") or current.get("tool_provenance") or {}).get(pending[0]) or {})
        orchestration_provenance = dict((current.get("orchestration_provenance") or {}).get(pending[0]) or {})
        lines.extend([
            "",
            "Current durable test contract (authoritative after compaction):",
            f"test_id: {pending[0]}",
            f"objective: {str(spec.get('objective') or '')[:1200]}",
            "required_interfaces: " + (", ".join(spec.get("required_interfaces") or []) or "none"),
            "required_orchestration_interfaces: " + (", ".join(spec.get("required_orchestration_interfaces") or []) or "none"),
            "required_observations: " + (", ".join(spec.get("required_observations") or []) or "none"),
            "pass_requirements: " + (json.dumps(spec.get("pass_requirements") or [], ensure_ascii=False, sort_keys=True)[:1400]),
            "prior_attempt_requirements: " + (json.dumps(spec.get("prior_attempt_requirements") or [], ensure_ascii=False, sort_keys=True)[:1200]),
            f"evidence_scope: {spec.get('evidence_scope') or 'run_scope'}",
            f"min_evidence_refs: {int(spec.get('min_evidence_refs') or 0)}",
            f"min_candidate_refs: {int(spec.get('min_candidate_refs') or 0)}",
            f"native_tool_policy: {spec.get('native_tool_policy') or 'unspecified'}",
            "allowed_native_tools: " + (", ".join(spec.get("allowed_native_tools") or []) or "none declared"),
            "native_tool_limits: " + json.dumps(spec.get("native_tool_limits") or {}, ensure_ascii=False, sort_keys=True),
            "interface_limits: " + json.dumps(spec.get("interface_limits") or {}, ensure_ascii=False, sort_keys=True),
            "orchestration_interface_limits: " + json.dumps(spec.get("orchestration_interface_limits") or {}, ensure_ascii=False, sort_keys=True),
            "forbidden_tool_classes: " + (", ".join(spec.get("forbidden_tool_classes") or []) or "none"),
            "allowed_actions labels: " + (", ".join(spec.get("allowed_actions") or []) or "none"),
            "forbidden_actions labels: " + (", ".join(spec.get("forbidden_actions") or []) or "none"),
            f"stop_after: {bool(spec.get('stop_after'))}",
            "observed_execution_provenance: " + json.dumps(execution_provenance.get("invocations") or {}, ensure_ascii=False, sort_keys=True)[:1600],
            "observed_orchestration_provenance: " + json.dumps(orchestration_provenance.get("invocations") or {}, ensure_ascii=False, sort_keys=True)[:1200],
        ])
    lines.extend([
        "",
        "After compaction, call acceptance_run_next before acting. After each acceptance test, record its structured evidence immediately with acceptance_run_record before moving to the next test. Machine-observed tool/evidence protocol may downgrade a claimed PASS. Final reports must aggregate acceptance_run_status rather than rely on compacted chat memory.",
    ])
    return "\n".join(lines)[:max_chars]
