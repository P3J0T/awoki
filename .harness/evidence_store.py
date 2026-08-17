from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import project_workspace

SCHEMA = "awoki-evidence-artifact/v1"
MAX_CANDIDATES = 200
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_SELECTOR_DEPTH = 12
MAX_PAGE = 100
DEFAULT_MAX_CHARS = 20_000
_REF_RE = re.compile(r"^ev_[0-9a-f]{24}$")
MAX_CAPTURE_PROVENANCE = 64

_CANDIDATE_FIELDS = (
    "symbol_id", "chunk_id", "path", "start_line", "end_line", "symbol_name",
    "qualified_name", "symbol", "symbol_kind", "language", "authority_class",
    "retrieval_backends", "fts_rank", "qdrant_rank", "fused_rank",
    "pre_rerank_rank", "post_refinement_rank", "composed_rank",
    "refinement_requalified", "refinement_parent_fused_rank",
    "composition_protected", "rerank_focus_lane_eligible",
    "rerank_focus_lane_signals", "rerank_focus_selection_order",
    "rerank_structural_lane_eligible", "rerank_structural_selection_order",
    "rerank_selection_lane", "rerank_selected", "rerank_score_returned",
    "rerank_score", "rerank_rank", "final_rank", "final_score", "score",
)


def _now() -> str:
    return project_workspace.now_ts()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _dir(root: Path, project_id: str) -> Path:
    # The /raw/ path component intentionally keeps these artifacts outside Awoki's
    # registered-safe artifact/RAG path.  They are supporting evidence, not memory.
    return project_workspace.paths_for(root, project_id).artifacts_dir / "acceptance" / "raw" / "evidence"


def _path(root: Path, project_id: str, evidence_ref: str) -> Path:
    if not _REF_RE.match(str(evidence_ref or "")):
        raise ValueError("invalid evidence_ref")
    return _dir(root, project_id) / evidence_ref[:6] / f"{evidence_ref}.json.gz"


def _provenance_path(root: Path, project_id: str, evidence_ref: str) -> Path:
    if not _REF_RE.match(str(evidence_ref or "")):
        raise ValueError("invalid evidence_ref")
    return _dir(root, project_id) / "provenance" / f"{evidence_ref}.json"


@contextmanager
def _provenance_lock(path: Path) -> Iterator[None]:
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


def _session_key(session_id: str) -> str:
    return hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()[:24] if session_id else ""


def _capture_provenance(root: Path, project_id: str, evidence_ref: str) -> list[dict[str, Any]]:
    path = _provenance_path(root, project_id, evidence_ref)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = value.get("captures") if isinstance(value, dict) else []
    return [dict(row) for row in rows if isinstance(row, dict)][-MAX_CAPTURE_PROVENANCE:]


def _record_capture_provenance(
    root: Path,
    project_id: str,
    evidence_ref: str,
    *,
    run_id: str = "",
    test_id: str = "",
    session_id: str = "",
) -> None:
    """Record capture context separately from immutable content-addressed evidence.

    The same exact evidence payload may legitimately be captured in more than one
    acceptance run. Keeping provenance in a bounded sidecar preserves stable ev_
    identity while still allowing a test contract to require that evidence was
    actually captured during the current run.
    """
    clean_run = str(run_id or "").strip()[:200]
    clean_test = str(test_id or "").strip()[:160]
    session_key = _session_key(session_id)
    if not any((clean_run, clean_test, session_key)):
        return
    path = _provenance_path(root, project_id, evidence_ref)
    with _provenance_lock(path):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        captures = [dict(row) for row in (state.get("captures") or []) if isinstance(row, dict)]
        row = {
            "run_id": clean_run,
            "test_id": clean_test,
            "session_key": session_key,
            "captured_at": _now(),
        }
        identity = (row["run_id"], row["test_id"], row["session_key"])
        if not any((str(item.get("run_id") or ""), str(item.get("test_id") or ""), str(item.get("session_key") or "")) == identity for item in captures):
            captures.append(row)
        state = {
            "schema": "awoki-evidence-capture-provenance/v1",
            "evidence_ref": evidence_ref,
            "captures": captures[-MAX_CAPTURE_PROVENANCE:],
            "updated_at": _now(),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)




def _backend_observations(payload: Any) -> dict[str, Any]:
    """Extract small backend-health facts from captured Awoki results.

    Rich tool evidence remains in ``payload``; this metadata lets continuity and
    reliability checkpoints notice degraded retrieval without reopening the
    entire artifact. Only bounded scalar telemetry is copied.
    """
    if not isinstance(payload, dict):
        return {}
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    retrieval = details.get("retrieval") if isinstance(details.get("retrieval"), dict) else {}
    reranker = retrieval.get("reranker") if isinstance(retrieval.get("reranker"), dict) else {}
    result: dict[str, Any] = {}
    if reranker or any(key.startswith("rerank_") for key in retrieval):
        result["reranker"] = {
            "attempted": bool(retrieval.get("rerank_attempted", reranker.get("attempted"))),
            "applied": bool(retrieval.get("rerank_applied", reranker.get("applied"))),
            "backend": str(retrieval.get("rerank_backend", reranker.get("backend")) or "")[:120],
            "model": str(reranker.get("model") or "")[:240],
            "latency_ms": retrieval.get("rerank_latency_ms", reranker.get("latency_ms")),
            "timeout_seconds": retrieval.get("rerank_timeout_seconds", reranker.get("timeout_seconds")),
            "timeout_source": str(retrieval.get("rerank_timeout_source", reranker.get("timeout_source")) or "")[:120],
            "failure_class": str(retrieval.get("rerank_failure_class", reranker.get("failure_class")) or "")[:80],
            "retryable": bool(retrieval.get("rerank_retryable", reranker.get("retryable"))),
            "degraded": bool(retrieval.get("rerank_degraded", reranker.get("degraded"))),
            "scores_returned": retrieval.get("rerank_scores_returned_to_awoki", reranker.get("scores_returned_to_awoki")),
            "scores_requested": retrieval.get("rerank_results_requested_top_n", reranker.get("results_requested_top_n")),
            "reason": str(retrieval.get("rerank_reason", reranker.get("reason")) or "")[:500],
        }
    embedding_status = retrieval.get("embedding_status")
    if embedding_status is not None:
        result["embedding"] = {
            "attempted": bool(retrieval.get("embedding_attempted")),
            "status": str(embedding_status or "")[:80],
            "latency_ms": retrieval.get("embedding_latency_ms"),
        }
    return result

def _candidate_identity(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("symbol_id") or ""),
        str(row.get("chunk_id") or ""),
        str(row.get("path") or ""),
        str(row.get("start_line") or ""),
        str(row.get("qualified_name") or row.get("symbol") or row.get("symbol_name") or ""),
    ]
    return "|".join(parts)


def _candidate_index(payload: Any, *, scope_identity: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("hits")
    if not isinstance(rows, list) and isinstance(payload.get("result"), dict):
        rows = payload["result"].get("hits")
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    scope_key = _canonical_bytes(scope_identity)
    for raw in rows[:MAX_CANDIDATES]:
        if not isinstance(raw, dict):
            continue
        identity = _candidate_identity(raw)
        if not identity.strip("|"):
            continue
        candidate_id = "cand_" + hashlib.sha256(scope_key + b"|" + identity.encode("utf-8")).hexdigest()[:20]
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        row = {"candidate_id": candidate_id}
        for key in _CANDIDATE_FIELDS:
            if key in raw:
                value = raw.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    row[key] = value
                elif isinstance(value, list):
                    row[key] = [item for item in value[:32] if isinstance(item, (str, int, float, bool)) or item is None]
        result.append(row)
    return result


def put(
    root: Path,
    project_id: str,
    *,
    kind: str,
    tool: str,
    payload: Any,
    scope_identity: dict[str, Any],
    run_id: str = "",
    test_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Persist exact Awoki-produced evidence outside the compact acceptance ledger.

    The payload is local project evidence and may contain source/tool detail.  It is
    therefore written below artifacts/acceptance/raw/, never registered as RAG-safe.
    The artifact is content-addressed and immutable once written.
    """
    normalized = {
        "kind": str(kind or "awoki_tool_result")[:120],
        "tool": str(tool or "unknown")[:160],
        "project_id": str(project_id or "")[:160],
        "scope_identity": dict(scope_identity or {}),
        "payload": payload,
    }
    normalized_bytes = _canonical_bytes(normalized)
    if len(normalized_bytes) > MAX_ARTIFACT_BYTES:
        return {
            "status": "rejected",
            "reason": "evidence_artifact_too_large",
            "serialized_bytes": len(normalized_bytes),
            "max_bytes": MAX_ARTIFACT_BYTES,
        }
    artifact_digest = hashlib.sha256(normalized_bytes).hexdigest()
    payload_digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    evidence_ref = "ev_" + artifact_digest[:24]
    envelope = {
        "schema": SCHEMA,
        "evidence_ref": evidence_ref,
        "created_at": _now(),
        "kind": normalized["kind"],
        "tool": normalized["tool"],
        "project_id": normalized["project_id"],
        "scope_identity": normalized["scope_identity"],
        "artifact_sha256": artifact_digest,
        "payload_sha256": payload_digest,
        "candidate_index": _candidate_index(payload, scope_identity=normalized["scope_identity"]),
        "backend_observations": _backend_observations(payload),
        "payload": payload,
    }
    path = _path(root, project_id, evidence_ref)
    if path.exists():
        existing = _load(root, project_id, evidence_ref)
        integrity_error = _integrity_error(existing, evidence_ref) if existing else "existing_artifact_unreadable"
        if integrity_error:
            return {
                "status": "integrity_error", "reason": integrity_error,
                "evidence_ref": evidence_ref, "storage_class": "project_raw_non_rag",
            }
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _canonical_bytes(envelope)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_bytes(gzip.compress(data, compresslevel=6, mtime=0))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    _record_capture_provenance(
        root, project_id, evidence_ref, run_id=run_id, test_id=test_id, session_id=session_id
    )
    returned_candidates = list(envelope["candidate_index"])[:40]
    return {
        "status": "stored" if path.exists() else "error",
        "evidence_ref": evidence_ref,
        "kind": envelope["kind"],
        "tool": envelope["tool"],
        "artifact_sha256": artifact_digest,
        "payload_sha256": payload_digest,
        "candidate_index": returned_candidates,
        "candidate_count": len(envelope["candidate_index"]),
        "candidate_index_truncated": len(envelope["candidate_index"]) > len(returned_candidates),
        "backend_observations": envelope.get("backend_observations") or {},
        "storage_class": "project_raw_non_rag",
    }


def _load(root: Path, project_id: str, evidence_ref: str) -> dict[str, Any]:
    try:
        path = _path(root, project_id, evidence_ref)
        data = gzip.decompress(path.read_bytes())
        value = json.loads(data.decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError, gzip.BadGzipFile):
        return {}


def _integrity_error(value: dict[str, Any], evidence_ref: str) -> str:
    if not value:
        return ""
    normalized = {
        "kind": str(value.get("kind") or "")[:120],
        "tool": str(value.get("tool") or "")[:160],
        "project_id": str(value.get("project_id") or "")[:160],
        "scope_identity": dict(value.get("scope_identity") or {}),
        "payload": value.get("payload"),
    }
    artifact_digest = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
    payload_digest = hashlib.sha256(_canonical_bytes(value.get("payload"))).hexdigest()
    if str(value.get("evidence_ref") or "") != evidence_ref:
        return "evidence_ref_mismatch"
    if evidence_ref != "ev_" + artifact_digest[:24]:
        return "content_address_mismatch"
    if str(value.get("artifact_sha256") or "") != artifact_digest:
        return "artifact_digest_mismatch"
    if str(value.get("payload_sha256") or "") != payload_digest:
        return "payload_digest_mismatch"
    return ""


def metadata(root: Path, project_id: str, evidence_ref: str) -> dict[str, Any]:
    value = _load(root, project_id, evidence_ref)
    if not value or value.get("schema") != SCHEMA:
        return {"status": "not_found", "evidence_ref": evidence_ref, "project_id": project_id}
    integrity_error = _integrity_error(value, evidence_ref)
    if integrity_error:
        return {"status": "integrity_error", "reason": integrity_error, "evidence_ref": evidence_ref, "project_id": project_id}
    captures = _capture_provenance(root, project_id, evidence_ref)
    return {
        "status": "ok",
        "schema": value.get("schema"),
        "evidence_ref": value.get("evidence_ref"),
        "kind": value.get("kind"),
        "tool": value.get("tool"),
        "project_id": value.get("project_id"),
        "scope_identity": value.get("scope_identity"),
        "artifact_sha256": value.get("artifact_sha256"),
        "payload_sha256": value.get("payload_sha256"),
        "created_at": value.get("created_at"),
        "candidate_index": value.get("candidate_index") or [],
        "candidate_count": len(value.get("candidate_index") or []),
        "backend_observations": _backend_observations(value.get("payload")),
        "capture_provenance": captures,
        "capture_run_ids": sorted({str(row.get("run_id") or "") for row in captures if str(row.get("run_id") or "")}),
        "storage_class": "project_raw_non_rag",
    }


def candidates(root: Path, project_id: str, evidence_ref: str) -> list[dict[str, Any]]:
    meta = metadata(root, project_id, evidence_ref)
    return [dict(row) for row in (meta.get("candidate_index") or []) if isinstance(row, dict)] if meta.get("status") == "ok" else []


def _select(value: Any, selector: str) -> tuple[bool, Any, str]:
    current = value
    if not selector.strip():
        return True, current, ""
    parts = [part for part in selector.split(".") if part != ""]
    if len(parts) > MAX_SELECTOR_DEPTH:
        return False, None, "selector_too_deep"
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                return False, None, f"missing_key:{part}"
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            if idx < 0 or idx >= len(current):
                return False, None, f"index_out_of_range:{part}"
            current = current[idx]
        else:
            return False, None, f"selector_not_applicable:{part}"
    return True, current, ""


def get(
    root: Path,
    project_id: str,
    evidence_ref: str,
    *,
    selector: str = "payload",
    offset: int = 0,
    limit: int = 20,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict[str, Any]:
    value = _load(root, project_id, evidence_ref)
    if not value or value.get("schema") != SCHEMA:
        return {"status": "not_found", "evidence_ref": evidence_ref, "project_id": project_id}
    integrity_error = _integrity_error(value, evidence_ref)
    if integrity_error:
        return {"status": "integrity_error", "reason": integrity_error, "evidence_ref": evidence_ref, "project_id": project_id}
    ok, selected, reason = _select(value, selector)
    if not ok:
        return {
            "status": "rejected",
            "reason": reason,
            "evidence_ref": evidence_ref,
            "selector": selector,
            "available_top_level_keys": sorted(value.keys()),
        }
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 20), MAX_PAGE))
    max_chars = max(1_000, min(int(max_chars or DEFAULT_MAX_CHARS), 100_000))
    base = {
        "status": "ok",
        "evidence_ref": evidence_ref,
        "project_id": project_id,
        "selector": selector,
        "artifact_sha256": value.get("artifact_sha256"),
        "payload_sha256": value.get("payload_sha256"),
        "scope_identity": value.get("scope_identity"),
        "kind": value.get("kind"),
        "tool": value.get("tool"),
        "created_at": value.get("created_at"),
        "backend_observations": _backend_observations(value.get("payload")),
        "capture_provenance": _capture_provenance(root, project_id, evidence_ref),
        "storage_class": "project_raw_non_rag",
    }
    if isinstance(selected, list):
        page = selected[offset:offset + limit]
        return {
            **base,
            "kind": "list",
            "offset": offset,
            "returned": len(page),
            "total": len(selected),
            "next_offset": offset + len(page) if offset + len(page) < len(selected) else None,
            "value": page,
        }
    if isinstance(selected, dict):
        serialized = _canonical_bytes(selected)
        if len(serialized) > max_chars:
            return {
                **base,
                "status": "too_large",
                "kind": "dict",
                "serialized_chars": len(serialized),
                "available_keys": sorted(str(key) for key in selected.keys()),
                "reason": "Selected object is intentionally not dumped into MCP context; request a narrower selector.",
            }
        return {**base, "kind": "dict", "value": selected}
    if isinstance(selected, str):
        return {
            **base,
            "kind": "string",
            "value": selected[:max_chars],
            "truncated": len(selected) > max_chars,
            "total_chars": len(selected),
        }
    return {**base, "kind": type(selected).__name__, "value": selected}
