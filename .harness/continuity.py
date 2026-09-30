from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import safety

CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}
INDEX_POLICIES = {"safe", "metadata_only", "no_rag"}
SENSITIVITY_LEVELS = {"public", "project", "sensitive", "secret"}

LEGACY_MEMORY_FILES = {
    "facts.jsonl": "fact",
    "findings.jsonl": "finding",
    "hypotheses.jsonl": "question",
    "decisions.jsonl": "decision",
    "events.jsonl": "event",
    "pending.jsonl": "possible_continuation",
}

KNOWLEDGE_KINDS = {
    "fact", "finding", "observation", "discovery", "decision", "correction",
    "artifact", "reflection", "continuity_reflection",
}
UNCERTAINTY_KINDS = {"question", "hypothesis", "uncertainty", "contradiction"}
CONTINUATION_KINDS = {"possible_continuation", "direction", "pending"}
KNOWN_KINDS = KNOWLEDGE_KINDS | UNCERTAINTY_KINDS | CONTINUATION_KINDS | {
    "event", "checkpoint", "conversation_note", "project_created", "parse_error",
}
SOURCE_KEYS = {
    "type", "path", "ref", "uri", "id", "label", "title", "description",
    "location", "record_id", "run_id", "repo", "source_id", "commit",
    "line", "line_start", "line_end", "hash",
}
SOURCE_NUMERIC_KEYS = {"line", "line_start", "line_end"}
SOURCE_REFERENCE_PREFIXES = tuple(dict.fromkeys((*safety.ALLOWED_REFERENCE_PREFIXES, "burp-run://", "burp-live://", "project://")))
EVIDENCE_BACKED_KINDS = {"finding", "discovery"}


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, obj: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(dict(obj), ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            out.append({
                "id": f"parse_error_{line_no}",
                "timestamp": "",
                "kind": "parse_error",
                "summary": f"Invalid JSONL at {path}:{line_no}: {exc}",
                "index_policy": "no_rag",
                "_source_file": str(path),
                "_line": line_no,
            })
            continue
        if isinstance(item, dict):
            item.setdefault("_source_file", str(path))
            item.setdefault("_line", line_no)
            out.append(item)
    return out


def _clean_text(value: Any, max_chars: int = 20_000) -> str:
    text = str(value or "").strip()
    return text[:max_chars]


def _normalize_confidence(value: Any) -> str:
    text = str(value or "medium").strip().lower()
    aliases = {
        "confirmed": "high", "observed": "high", "reviewed": "high",
        "likely": "medium", "hypothesis": "low", "unverified": "low",
    }
    text = aliases.get(text, text)
    return text if text in CONFIDENCE_LEVELS else "unknown"


def _normalize_sensitivity(value: Any) -> str:
    text = str(value or "project").strip().lower()
    aliases = {"normal": "project", "internal": "project", "private": "sensitive"}
    text = aliases.get(text, text)
    return text if text in SENSITIVITY_LEVELS else "project"


def _normalize_index_policy(value: Any, sensitivity: str) -> str:
    text = str(value or "safe").strip().lower()
    if sensitivity in {"sensitive", "secret"}:
        return "no_rag"
    return text if text in INDEX_POLICIES else "no_rag"


def _normalize_source_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        return ""
    return candidate.as_posix()[:1_000]


def normalize_sources(sources: Iterable[Any] | None) -> list[dict[str, Any]]:
    """Normalize source references into a small, non-secret-bearing schema.

    Source objects are references, not arbitrary metadata containers. Unknown
    keys and nested values are dropped so adapters cannot accidentally smuggle
    sensitive values or raw tool output into continuity or generated views.
    """
    normalized: list[dict[str, Any]] = []
    for source in sources or []:
        if isinstance(source, str):
            raw = source.strip()
            if not raw:
                continue
            item = {"type": "reference", "id": raw} if raw.startswith("ev_") else {
                "type": "reference" if raw.startswith(SOURCE_REFERENCE_PREFIXES) else "file",
                "ref" if raw.startswith(SOURCE_REFERENCE_PREFIXES) else "path": raw,
            }
        elif isinstance(source, Mapping):
            # code_source_window names its handle evidence_ref. Accept that
            # natural object spelling as well as id/ref or a plain handle.
            # Conflicting aliases must never select one apparently-current ref.
            aliases = [str(source[key]).strip() for key in ("id", "ref", "evidence_ref")
                       if source.get(key) not in (None, "")]
            if "evidence_ref" in source or any(value.startswith("ev_") for value in aliases):
                valid = len(set(aliases)) == 1 and aliases[0].startswith("ev_")
                source = {**source, "id": aliases[0] if valid else "ev_invalid_conflicting_source_handles"}
            item = {}
            for key, value in source.items():
                clean_key = str(key).strip()
                if clean_key not in SOURCE_KEYS or value in (None, "", [], {}):
                    continue
                if clean_key in SOURCE_NUMERIC_KEYS:
                    try:
                        item[clean_key] = int(value)
                    except (TypeError, ValueError):
                        continue
                elif isinstance(value, (str, int, float, bool)):
                    item[clean_key] = str(value).strip() if isinstance(value, str) else value
        else:
            continue
        if not item:
            continue
        if not item.get("type"):
            item["type"] = "file" if item.get("path") else "reference"
        if item.get("path"):
            safe_path = _normalize_source_path(item["path"])
            if safe_path:
                item["path"] = safe_path
            else:
                item.pop("path", None)
        if item.get("ref"):
            clean_ref = str(item["ref"]).strip()[:1_000]
            if clean_ref.startswith("ev_"):
                item.setdefault("id", clean_ref)
                item.pop("ref", None)
            elif clean_ref.startswith(SOURCE_REFERENCE_PREFIXES):
                item["ref"] = clean_ref
            else:
                item.pop("ref", None)
        if item.get("uri"):
            clean_uri = str(item["uri"]).strip()[:1_000]
            if "\n" in clean_uri or "\r" in clean_uri:
                item.pop("uri", None)
            else:
                item["uri"] = clean_uri
        if not any(key in item for key in ("path", "ref", "uri", "id", "record_id", "run_id", "repo", "commit", "location")):
            continue
        if item not in normalized:
            normalized.append(item)
    return normalized[:100]


def continuity_fingerprint(record: Mapping[str, Any]) -> str:
    payload = {
        "kind": record.get("kind"),
        "summary": record.get("summary"),
        "details": record.get("details"),
        "sources": record.get("sources"),
        "uncertainty": record.get("uncertainty"),
        "likely_continuation": record.get("likely_continuation"),
        "state": record.get("state"),
        "supersedes": record.get("supersedes"),
        "confidence": record.get("confidence"),
        "sensitivity": record.get("sensitivity"),
        "index_policy": record.get("index_policy"),
        "tags": record.get("tags"),
    }
    # Preserve old fingerprints when absent, but do not deduplicate notes that
    # explicitly derive from different parents and erase their lineage.
    based_on = (record.get("metadata") or {}).get("based_on")
    if based_on:
        payload["based_on"] = based_on
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def make_record(
    project_id: str,
    summary: str,
    *,
    kind: str = "observation",
    details: str = "",
    sources: Iterable[Any] | None = None,
    confidence: str = "medium",
    sensitivity: str = "project",
    index_policy: str = "safe",
    tags: Iterable[str] | None = None,
    uncertainty: Iterable[str] | None = None,
    likely_continuation: str = "",
    supersedes: Iterable[str] | None = None,
    state: str = "",
    metadata: Mapping[str, Any] | None = None,
    timestamp: str = "",
    record_id: str = "",
    allow_sensitive_plaintext: bool = False,
) -> dict[str, Any]:
    summary = _clean_text(summary, 2_000)
    if not summary:
        raise ValueError("continuity summary cannot be empty")
    if allow_sensitive_plaintext:
        safe_summary, summary_changed = summary, False
        safe_details, details_changed = str(details or ""), False
        safe_sources, sources_changed = list(sources or []), False
        safe_tags, tags_changed = list(tags or []), False
        safe_uncertainty, uncertainty_changed = list(uncertainty or []), False
        safe_continuation, continuation_changed = str(likely_continuation or ""), False
        safe_metadata, metadata_changed = dict(metadata or {}), False
    else:
        safe_summary, summary_changed = safety.redact_analysis_text(summary)
        safe_details, details_changed = safety.redact_analysis_text(details)
        safe_sources, sources_changed = safety.redact_analysis_nested(list(sources or []))
        safe_tags, tags_changed = safety.redact_analysis_nested(list(tags or []))
        safe_uncertainty, uncertainty_changed = safety.redact_analysis_nested(list(uncertainty or []))
        safe_continuation, continuation_changed = safety.redact_analysis_text(likely_continuation)
        safe_metadata, metadata_changed = safety.redact_analysis_nested(dict(metadata or {}))
    redacted = any((
        summary_changed, details_changed, sources_changed, tags_changed,
        uncertainty_changed, continuation_changed, metadata_changed,
    ))
    original_kind = _clean_text(kind, 80) or "observation"
    kind = original_kind.lower().replace(" ", "_").replace("-", "_") or "observation"
    normalized_sources = normalize_sources(safe_sources)
    normalized_confidence = _normalize_confidence(confidence)
    confidence_adjustment = ""
    if normalized_confidence == "high" and kind in EVIDENCE_BACKED_KINDS and not normalized_sources:
        normalized_confidence = "medium"
        confidence_adjustment = "downgraded_missing_source"
    # A redacted value inside an analysis record does not make the surrounding
    # finding non-retrievable. Only explicit sensitive-plaintext capture or the
    # caller's requested policy changes retrieval scope.
    normalized_sensitivity = _normalize_sensitivity("secret" if allow_sensitive_plaintext else sensitivity)
    normalized_policy = _normalize_index_policy("no_rag" if allow_sensitive_plaintext else index_policy, normalized_sensitivity)
    record: dict[str, Any] = {
        "id": record_id or f"cont_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{uuid.uuid4().hex[:10]}",
        "timestamp": timestamp or now_ts(),
        "project_id": project_id,
        "kind": kind,
        "summary": _clean_text(safe_summary, 2_000),
        "details": _clean_text(safe_details),
        "sources": normalized_sources,
        "confidence": normalized_confidence,
        "sensitivity": normalized_sensitivity,
        "index_policy": normalized_policy,
        "tags": sorted({str(t).strip() for t in (safe_tags or []) if str(t).strip()}),
        "uncertainty": [_clean_text(v, 1_000) for v in (safe_uncertainty or []) if _clean_text(v, 1_000)],
        "likely_continuation": _clean_text(safe_continuation, 2_000),
        "supersedes": [str(v).strip() for v in (supersedes or []) if str(v).strip()],
    }
    if original_kind != kind or kind not in KNOWN_KINDS:
        record["original_kind"] = original_kind
    if state:
        record["state"] = _clean_text(state, 80).lower()
    metadata_out = dict(safe_metadata or {})
    if confidence_adjustment:
        metadata_out["confidence_adjustment"] = confidence_adjustment
    if metadata_out:
        record["metadata"] = metadata_out
    if redacted:
        record["redacted"] = True
    if allow_sensitive_plaintext:
        record["explicit_sensitive_plaintext"] = True
    record["fingerprint"] = continuity_fingerprint(record)
    return record


def append_record(path: Path, record: Mapping[str, Any], dedupe_recent: int = 40) -> dict[str, Any]:
    """Append a continuity record with process-safe recent deduplication.

    The duplicate check and write must happen while holding the same file lock.
    Otherwise two OpenCode sessions can both observe "no duplicate" and append
    the same automatic reflection concurrently.
    """
    item = dict(record)
    fingerprint = continuity_fingerprint(item)
    item["fingerprint"] = fingerprint
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            parsed: list[dict[str, Any]] = []
            for line in handle.read().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    parsed.append(row)
            recent = parsed[-max(1, dedupe_recent):]
            # Recompute so pre-upgrade fingerprints remain compatible without
            # rewriting the append-only history. Never dedupe against retired memory.
            active_ids = {str(r.get("id")) for r in active_records(parsed)}
            duplicate = next((r for r in reversed(recent) if str(r.get("id")) in active_ids
                              and continuity_fingerprint(r) == fingerprint), None)
            if duplicate:
                return {**duplicate, "_write_status": "duplicate_skipped"}
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return {**item, "_write_status": "appended"}
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _legacy_summary(row: Mapping[str, Any], default_kind: str) -> str:
    for key in ("summary", "title", "text", "hypothesis", "next_action", "event_type", "kind"):
        value = _clean_text(row.get(key), 2_000)
        if value:
            return value
    return f"Legacy {default_kind} record"


def legacy_to_record(project_id: str, row: Mapping[str, Any], default_kind: str) -> dict[str, Any]:
    kind = str(row.get("kind") or default_kind)
    if kind == "pending":
        kind = "possible_continuation"
    elif kind == "hypothesis":
        kind = "question"
    sources: list[Any] = []
    for value in row.get("related_files", []) if isinstance(row.get("related_files"), list) else []:
        sources.append(value)
    source_file = row.get("_source_file")
    if source_file:
        sources.append({"type": "legacy_memory", "path": f"memory/{Path(str(source_file)).name}", "line": row.get("_line")})
    details = row.get("evidence") or row.get("reason") or row.get("note") or ""
    likely = row.get("next_action") or ""
    record_id = str(row.get("continuity_id") or row.get("id") or "")
    if not record_id:
        seed = f"{source_file}:{row.get('_line')}:{_legacy_summary(row, default_kind)}"
        record_id = "legacy_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return make_record(
        project_id,
        _legacy_summary(row, default_kind),
        kind=kind,
        details=str(details),
        sources=sources,
        confidence=str(row.get("confidence") or "unknown"),
        sensitivity=str(row.get("sensitivity") or "project"),
        index_policy="no_rag" if row.get("sensitivity") in {"sensitive", "secret"} else "safe",
        tags=row.get("tags") if isinstance(row.get("tags"), list) else [],
        likely_continuation=str(likely),
        state=str(row.get("status") or ""),
        timestamp=str(row.get("timestamp") or row.get("created_at") or row.get("updated_at") or ""),
        record_id=record_id,
        metadata={"legacy": True, "legacy_kind": row.get("kind") or default_kind},
    )


def load_records(memory_dir: Path, project_id: str, include_legacy: bool = True, *,
                 canonical_records: Iterable[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    canonical_path = memory_dir / "continuity.jsonl"
    canonical = read_jsonl(canonical_path) if canonical_records is None else [dict(row) for row in canonical_records]
    # A caller may provide one validated snapshot instead of reopening the
    # journal. Keep canonical append order when timestamps coincide.
    if canonical_records is not None:
        for index, row in enumerate(canonical, 1):
            row.setdefault("_source_file", str(canonical_path))
            row.setdefault("_line", index)
    canonical_ids = {str(r.get("id")) for r in canonical if r.get("id")}
    records = list(canonical)
    if include_legacy:
        for filename, default_kind in LEGACY_MEMORY_FILES.items():
            for row in read_jsonl(memory_dir / filename):
                if row.get("kind") == "pending_resolution":
                    continue
                linked = str(row.get("continuity_id") or "")
                if linked and linked in canonical_ids:
                    continue
                records.append(legacy_to_record(project_id, row, default_kind))
    # Preserve append order for records created within the same second. Sorting
    # by the random ID suffix can place a later record before the handoff
    # baseline and make "changes since handoff" disappear.
    records.sort(key=lambda r: (
        str(r.get("timestamp") or r.get("created_at") or ""),
        str(r.get("_source_file") or ""),
        int(r.get("_line") or 0),
        str(r.get("id") or ""),
    ))
    return records


def active_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(r) for r in records]
    superseded: set[str] = set()
    for row in rows:
        superseded.update(str(v) for v in row.get("supersedes", []) if v)
    return [r for r in rows if str(r.get("id")) not in superseded]


def _read_goal_records(memory_dir: Path, project_id: str) -> list[dict[str, Any]]:
    """Read canonical state strictly: an unreadable journal is not an empty goal.

    Use the append writer's lock so a concurrent partial line never looks like a
    missing direction. Legacy suggestions are intentionally not inferred goals.
    """
    with (memory_dir / "continuity.jsonl").open("r", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            rows = [json.loads(line) for line in handle if line.strip()]
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("project_id") != project_id:
            raise ValueError("Invalid canonical record scope")
        ref = row.get("id")
        if not isinstance(ref, str) or not ref or len(ref) > 160 or any(c.isspace() for c in ref) or ref in seen:
            raise ValueError("Invalid canonical record identity")
        seen.add(ref)
        for key in ("kind", "summary"):
            if not isinstance(row.get(key), str):
                raise ValueError("Invalid canonical record content")
        for key in ("tags", "uncertainty", "sources"):
            if key in row and not isinstance(row[key], list):
                raise ValueError("Invalid canonical record collection")
        if "metadata" in row and not isinstance(row["metadata"], dict):
            raise ValueError("Invalid canonical record metadata")
        if not isinstance(row.get("supersedes", []), list) or not all(isinstance(v, str) for v in row.get("supersedes", [])):
            raise ValueError("Invalid supersession links")
    return rows


def _direction_lineage(records: list[dict[str, Any]]) -> set[str]:
    successors: dict[str, set[str]] = {}
    for row in records:
        for previous in row.get("supersedes") or []:
            successors.setdefault(previous, set()).add(str(row["id"]))
    related: set[str] = set()
    pending = [str(row["id"]) for row in records if row.get("kind") == "direction"]
    while pending:
        ref = pending.pop()
        if ref not in related:
            related.add(ref)
            pending.extend(successors.get(ref, ()))
    # A malformed cycle must not make every direction disappear as 'retired'.
    incoming = {ref: 0 for ref in related}
    for previous in related:
        for ref in successors.get(previous, ()):
            incoming[ref] += 1
    pending = [ref for ref, count in incoming.items() if not count]
    visited = 0
    while pending:
        ref = pending.pop()
        visited += 1
        for successor in successors.get(ref, ()):
            incoming[successor] -= 1
            if not incoming[successor]:
                pending.append(successor)
    if visited != len(related):
        raise ValueError("Cyclic direction supersession")
    return related


def direction_fingerprint(memory_dir: Path, project_id: str) -> dict[str, str]:
    """Opaque binding for queued work, including private and closed revisions.

    This is an internal invalidation token, never a label or proof of intent.
    Observation-only changes do not cancel an otherwise unchanged direction.
    """
    try:
        rows = _read_goal_records(memory_dir, project_id)
        related = _direction_lineage(rows)
        entries = sorted((str(row["id"]), continuity_fingerprint(row)) for row in rows if str(row["id"]) in related)
        payload = json.dumps([project_id, entries], ensure_ascii=False, separators=(",", ":"))
        return {"status": "known", "fingerprint": hashlib.sha256(payload.encode("utf-8")).hexdigest()}
    except (OSError, ValueError, TypeError, UnicodeError):
        return {"status": "unknown"}


def _view_safe(record: Mapping[str, Any]) -> bool:
    return (str(record.get("index_policy") or "safe").lower() != "no_rag"
            and str(record.get("sensitivity") or "project").lower() not in {"sensitive", "secret"})


def goal_projection(records: Iterable[Mapping[str, Any]], *, project_id: str) -> dict[str, Any]:
    """Bounded navigation to saved direction; no goal inference or new ledger.

    A lone saved direction still needs reconciliation with the current user.
    Competing directions, hidden direction state and kind-changing revisions
    must never silently select an apparently authoritative older goal.
    """
    rows = [dict(row) for row in records]
    active = active_records(rows)
    closed = {"done", "complete", "completed", "closed", "resolved", "superseded", "retired", "cancelled", "canceled"}
    open_rows = [row for row in active if str(row.get("state") or "").lower() not in closed]
    lineage = _direction_lineage(rows)
    safe = [row for row in open_rows if _view_safe(row)]
    directions = [row for row in safe if row.get("kind") == "direction"]
    changed_kind = [row for row in safe if str(row.get("id")) in lineage and row.get("kind") != "direction"]
    # Native clients may save a finding with explicit state="checkpoint".
    # Preserve that navigation intent without inferring a goal from its prose.
    checkpoints = [row for row in safe if row.get("kind") == "checkpoint"
                   or str(row.get("state") or "").lower() == "checkpoint"
                   or "investigation-checkpoint" in (row.get("tags") or [])]
    hidden_direction = any(str(row.get("id")) in lineage and not _view_safe(row) for row in open_rows)
    status = "unknown" if hidden_direction else "ambiguous" if len(directions) > 1 or changed_kind else "saved" if directions else "none"

    def preview(row: Mapping[str, Any]) -> dict[str, Any]:
        summary = safety.redact_analysis_text(str(row.get("summary") or ""))[0]
        return {
            "record_id": row["id"], "kind": row.get("kind"),
            "summary": summary[:240], "summary_truncated": len(summary) > 240,
            "details_available": bool(row.get("details") or row.get("uncertainty") or row.get("likely_continuation")),
        }

    # IDs come first and are never cut to satisfy a text budget. Details and
    # source evidence stay in the original note, with an exact local read route.
    selected_directions = list(reversed(directions))[:3]
    selected_checkpoints = list(reversed(checkpoints))[:2]
    selected_revisions = list(reversed(changed_kind))[:2]
    selected = selected_directions + selected_checkpoints + selected_revisions
    refs = list(dict.fromkeys(str(row["id"]) for row in selected))
    result: dict[str, Any] = {
        "project_id": project_id, "status": status,
        "directions": [preview(row) for row in selected_directions],
        "checkpoints": [preview(row) for row in selected_checkpoints],
        "unresolved_revisions": [preview(row) for row in selected_revisions],
        "omitted": {"directions": max(0, len(directions) - 3), "checkpoints": max(0, len(checkpoints) - 2), "revisions": max(0, len(changed_kind) - 2)},
        "next_calls": [
            {"tool": "project_search", "arguments": {"name": project_id, "record_ids": refs[start:start + 3], "max_chars": 6000}}
            for start in range(0, len(refs), 3)
        ],
        "policy": "Saved notes are navigation, not current authority. Reconcile with the newest user direction; read exact details before rebuilding TODOs. Checkpoints do not create a goal.",
    }
    if status == "unknown":
        result["reason"] = "direction_state_not_projectable"
    elif status == "ambiguous":
        result["reason"] = "multiple_active_directions" if len(directions) > 1 else "direction_revision_changed_kind"
    return result


def load_goal_projection(memory_dir: Path, project_id: str) -> dict[str, Any]:
    try:
        return goal_projection(_read_goal_records(memory_dir, project_id), project_id=project_id)
    except (OSError, ValueError, TypeError, UnicodeError):
        return {
            "project_id": project_id, "status": "unknown", "reason": "canonical_state_unavailable",
            "directions": [], "checkpoints": [], "unresolved_revisions": [], "next_calls": [],
            "policy": "Recovery failed; this does not mean there is no goal. Keep the current request and unresolved work; retry only when the failure is resolved.",
        }


def goal_projection_lines(projection: Mapping[str, Any]) -> list[str]:
    """Render the same small navigation view for generated continuity files."""
    status = str(projection.get("status") or "unknown")
    messages = {
        "none": "No active safe direction is saved; exploration needs no invented goal.",
        "saved": "Saved direction; reconcile with the newest user request before continuing.",
        "ambiguous": "Direction needs reconciliation; no single goal is selected.",
        "unknown": "Direction recovery is incomplete; do not treat this as no goal.",
    }
    lines = [f"- {messages.get(status, messages['unknown'])}"]
    for key in ("directions", "checkpoints", "unresolved_revisions"):
        for row in projection.get(key) or []:
            # JSON escaping keeps multiline notes from impersonating structure.
            summary = json.dumps(str(row.get("summary") or ""), ensure_ascii=False)
            lines.append(f"- [{row['record_id']}] {row.get('kind')}: {summary}")
    if any(projection.get(key) for key in ("directions", "checkpoints", "unresolved_revisions")):
        lines.append(f"- Read exact details with project_search(name={json.dumps(str(projection.get('project_id') or ''))}, record_ids=[up to 3 IDs above per call]); details and caveats are not reproduced here.")
    if any(int(value) for value in (projection.get("omitted") or {}).values()):
        lines.append("- Additional direction/checkpoint candidates omitted; use targeted project_search before choosing a goal.")
    return lines


def render_goal_projection(projection: Mapping[str, Any], max_chars: int = 1200) -> str:
    """Whole-line compaction projection with an explicit omission boundary."""
    budget = max(256, int(max_chars))
    lines = goal_projection_lines(projection)
    omitted = "- More saved direction/checkpoint context omitted; recover exact notes through project_search before rebuilding TODOs."
    output: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        reserve = len(omitted) + 1 if index < len(lines) - 1 else 0
        if used + len(line) + 1 + reserve > budget:
            output.append(omitted)
            break
        output.append(line)
        used += len(line) + 1
    return "\n".join(output)


def record_line(record: Mapping[str, Any], max_chars: int = 240) -> str:
    summary = _clean_text(record.get("summary"), max_chars)
    confidence = str(record.get("confidence") or "unknown")
    suffix = f" (confidence: {confidence})" if confidence in {"low", "unknown"} else ""
    record_id = str(record.get("id") or "")
    caveats = [_clean_text(value, 160) for value in (record.get("uncertainty") or [])[:3]]
    warning = "; caveats: " + " | ".join(caveats) if caveats else ""
    if len(record.get("uncertainty") or []) > 3:
        warning += "; more caveats in record"
    if record.get("sources"):
        warning += "; saved sources, recheck freshness before relying on code claims"
    return f"- {summary}{suffix}{warning}" + (f" [{record_id}]" if record_id else "")


def source_label(source: Mapping[str, Any]) -> str:
    path = source.get("path") or source.get("ref") or source.get("id") or ""
    kind = source.get("type") or "source"
    line = source.get("line")
    repo = str(source.get("repo") or "")
    text = f"{kind}: `{repo + '/' if repo else ''}{path}`" if path else str(kind)
    return f"{text}:{line}" if line else text


def unique_sources(records: Iterable[Mapping[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in reversed(list(records)):
        for source in record.get("sources", []) or []:
            if not isinstance(source, Mapping):
                continue
            key = json.dumps(dict(source), ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(source))
            if len(out) >= limit:
                return out
    return out


def meaningful_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(r) for r in records
        if r.get("kind") not in {"event", "parse_error"}
        and str(r.get("state") or "").lower() not in {"done", "closed", "resolved", "superseded"}
    ]
