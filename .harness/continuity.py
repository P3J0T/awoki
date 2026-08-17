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
    "location", "record_id", "run_id", "repo", "commit",
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
            item = {
                "type": "reference" if raw.startswith(SOURCE_REFERENCE_PREFIXES) else "file",
                "ref" if raw.startswith(SOURCE_REFERENCE_PREFIXES) else "path": raw,
            }
        elif isinstance(source, Mapping):
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
            if clean_ref.startswith(SOURCE_REFERENCE_PREFIXES):
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
    }
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
    fingerprint = str(item.get("fingerprint") or continuity_fingerprint(item))
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
            duplicate = next((r for r in reversed(recent) if r.get("fingerprint") == fingerprint), None)
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


def load_records(memory_dir: Path, project_id: str, include_legacy: bool = True) -> list[dict[str, Any]]:
    canonical_path = memory_dir / "continuity.jsonl"
    canonical = read_jsonl(canonical_path)
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


def record_line(record: Mapping[str, Any], max_chars: int = 240) -> str:
    summary = _clean_text(record.get("summary"), max_chars)
    confidence = str(record.get("confidence") or "unknown")
    suffix = f" (confidence: {confidence})" if confidence in {"low", "unknown"} else ""
    return f"- {summary}{suffix}"


def source_label(source: Mapping[str, Any]) -> str:
    path = source.get("path") or source.get("ref") or source.get("id") or ""
    kind = source.get("type") or "source"
    line = source.get("line")
    text = f"{kind}: `{path}`" if path else str(kind)
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
