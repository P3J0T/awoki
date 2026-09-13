"""Small projections of canonical continuity, not another memory store.

Search previews are discovery. Exact reads page saved material without a model,
remote retrieval, or promotion of remembered statements into verified claims.
"""
from __future__ import annotations

from typing import Any

import safety
import continuity
import findings_review

BOUNDARY = "Saved observations, not verified claims. Read omitted details and caveats before drawing conclusions; source freshness is not behavioral proof."
ITEM_KEYS = {"summary", "details", "kind", "sources", "evidence_refs", "based_on", "confidence", "uncertainty", "tags", "supersedes", "state"}


def validate_items(items: Any) -> str:
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        return "items must contain 1–12 separate notes; each uses the usual capture fields."
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) - ITEM_KEYS:
            return f"Item {number}: unsupported fields; use summary, details, kind, sources, evidence_refs, based_on, confidence, uncertainty, tags, supersedes or state. Set sensitivity/index_policy at the top level. Preserve evidence_refs; do not remove them to retry."
        if not isinstance(item.get("summary"), str) or not item["summary"].strip() or len(item["summary"]) > 600:
            return f"Item {number}: summary must be 1–600 characters. Save one observation per item."
        if not isinstance(item.get("details", ""), str) or len(item.get("details", "")) > 2000:
            return f"Item {number}: details must be at most 2000 characters; use ordinary capture for a longer note."
        for field in ("kind", "state"):
            if field in item and not isinstance(item[field], str):
                return f"Item {number}: {field} must be text."
        if item.get("confidence") is not None and not isinstance(item["confidence"], str):
            return f"Item {number}: confidence must be text or omitted."
        for field in ("sources", "evidence_refs", "uncertainty", "tags", "supersedes"):
            values = item.get(field, [])
            if not isinstance(values, list) or len(values) > 12:
                return f"Item {number}: {field} must be a list of at most 12 values."
            if field != "sources" and any(not isinstance(value, str) or len(value) > 1000 for value in values):
                return f"Item {number}: {field} values must be text of at most 1000 characters."
            if field == "sources" and any(not isinstance(value, (str, dict)) for value in values):
                return f"Item {number}: sources must contain saved handles or reference objects."
        error = validate_evidence_refs(item.get("evidence_refs"))
        if error:
            return f"Item {number}: {error}"
        error = validate_based_on(item.get("based_on"))
        if error:
            return f"Item {number}: {error}"
    return ""


def validate_evidence_refs(refs: Any) -> str:
    if refs is not None and (not isinstance(refs, list) or len(refs) > 12
                            or any(not isinstance(ref, str) or not ref.startswith("ev_") or len(ref) > 200 for ref in refs)):
        return "evidence_refs must contain at most 12 exact saved ev_ handles. Keep plain paths in sources; never drop a handle to bypass validation."
    return ""


def source_binding(sources: list[dict[str, Any]]) -> dict[str, Any]:
    bound = sum(str(source.get("id") or "").startswith("ev_") for source in sources)
    return {"status": "bound" if bound and bound == len(sources) else "mixed" if bound else "unbound",
            "evidence_reference_count": bound, "claim_verified": False,
            "boundary": "Saved reference bindings only; not current source freshness or behavioral proof."}


def validate_based_on(refs: Any) -> str:
    if refs is not None and (not isinstance(refs, list) or len(refs) > 3
                            or any(not isinstance(ref, str) or not ref.startswith("cont_") or len(ref) > 200 for ref in refs)):
        return "based_on must contain at most 3 exact active safe cont_ record IDs from this project."
    return ""


def inherit_qualifications(records: dict[str, dict[str, Any]], refs: list[str], sources: list[Any],
                           uncertainty: list[str], confidence: str | None) -> dict[str, Any]:
    """Carry explicit parents' qualifications; never infer a parent from similarity.

    The caller supplies only current, safe project records and validates the
    merged source handles before writing. No journal is changed here.
    """
    unavailable = [ref for ref in dict.fromkeys(refs) if ref not in records]
    if unavailable:
        return {"status": "rejected", "written": 0, "unavailable_ids": unavailable,
                "reason": "based_on requires active safe notes in this project. Recover exact records; do not drop a missing/private/retired parent to retry."}
    parents = [records[ref] for ref in dict.fromkeys(refs)]
    # De-duplicate before checking the canonical 100-source limit. Do not let
    # normalize_sources silently trim an inherited binding.
    merged_sources = []
    for source in [*(s for row in parents for s in row.get("sources") or []), *sources]:
        for normalized in continuity.normalize_sources([source]):
            if normalized not in merged_sources:
                merged_sources.append(normalized)
    merged_uncertainty = list(dict.fromkeys([
        *(str(value) for row in parents for value in row.get("uncertainty") or []), *uncertainty,
    ]))
    if len(merged_sources) > 100 or len(merged_uncertainty) > 100:
        return {"status": "rejected", "written": 0,
                "reason": "Inherited qualifications exceed 100 sources or caveats. Split the follow-up; none were silently discarded."}
    order = {"low": 0, "medium": 1, "high": 2}
    ceiling = min((continuity._normalize_confidence(row.get("confidence")) for row in parents), key=order.get)
    requested = continuity._normalize_confidence(confidence) if confidence is not None else ceiling
    effective = min((requested, ceiling), key=order.get)
    return {"status": "ok", "sources": merged_sources, "uncertainty": merged_uncertainty,
            "confidence": effective, "based_on": list(dict.fromkeys(refs)),
            "preservation": {"based_on": list(dict.fromkeys(refs)), "confidence_ceiling": ceiling,
                             "claim_verified": False,
                             "boundary": "Inherited sources and caveats, not proof of this new wording. Resolve a caveat with an explicit correction + supersedes."}}


def project_record(record: dict[str, Any], project_id: str, *, section: str = "record", offset: int = 0,
                   limit: int = 6000) -> dict[str, Any]:
    """Bound detail strings and source/caveat pages, with exact continuation calls."""
    # Redact again for pre-upgrade/legacy records; omit arbitrary nested metadata.
    row, _ = safety.redact_analysis_nested({key: record[key] for key in (
        "id", "kind", "summary", "details", "confidence", "uncertainty", "sources",
        "supersedes", "state", "timestamp", "likely_continuation", "tags",
    ) if key in record})
    offset = max(0, int(offset))
    limit = max(256, min(int(limit), 20000))
    out = {key: row[key] for key in ("id", "kind", "summary", "confidence", "state", "timestamp") if key in row}
    out["summary"] = str(out.get("summary") or "")[:2000]
    next_calls = []
    sections = ("details", "uncertainty", "sources", "likely_continuation", "tags", "supersedes") if section == "record" else (section,)
    for field in sections:
        value = row.get(field, [] if field in {"uncertainty", "sources", "tags", "supersedes"} else "")
        start = 0 if section == "record" else offset
        page_size = 8 if isinstance(value, list) else limit
        out[field] = value[start:start + page_size]
        if start + page_size < len(value):
            next_calls.append({"record_ids": [row["id"]], "name": project_id,
                               "section": field, "offset": start + page_size, "max_chars": limit})
    out["complete"] = not next_calls
    out["section"] = section
    out["offset"] = offset if section != "record" else 0
    out["next_calls"] = next_calls
    # A details-only read used to hide separately stored qualifications. Keep
    # a bounded qualification envelope on every page, without adding cross-page
    # next_calls that could loop when a caller drains a section's queue.
    caveats = list(row.get("uncertainty") or [])
    out["qualifications"] = {
        "uncertainty": caveats[:3], "uncertainty_count": len(caveats),
        "uncertainty_state": "recorded" if caveats else "not_recorded; not proof that none exist",
        "complete": len(caveats) <= 3,
        "source_binding": source_binding(continuity.normalize_sources(row.get("sources") or [])),
    }
    review_links = findings_review.memory_links(continuity.normalize_sources(row.get("sources") or []), project_id)
    if review_links:
        out["qualifications"]["findings_reviews"] = review_links[:3]
        out["qualifications"]["findings_review_count"] = len(review_links)
    if len(caveats) > 3:
        out["qualifications"]["read_all"] = {"name": project_id, "record_ids": [row["id"]], "section": "uncertainty"}
    out["followup_capture"] = {"tool": "project_capture", "arguments": {"name": project_id, "based_on": [row["id"]]},
                               "instruction": "For a follow-up to this note, add a summary. Its sources/caveats are retained automatically; this does not verify the new claim."}
    return out


def canonical_hits(hits: list[dict[str, Any]], records: dict[str, dict[str, Any]], project_id: str) -> list[dict[str, Any]]:
    """Replace derived previews with current safe memory; drop retired/private IDs."""
    out = []
    for hit in hits:
        record_id = str((hit.get("metadata") or {}).get("id") or "")
        if not record_id.startswith("cont_"):
            out.append(hit)
            continue
        row = records.get(record_id)
        if row is None:
            continue
        projected = project_record(row, project_id, limit=700)
        # Reconciliation history is control-plane data, not another copy of the
        # previous note to inject into a small model's context.
        out.append({**hit, "scope": "project", "project_id": project_id,
                    "record_id": record_id, "title": projected["summary"],
                    "preview": (projected["summary"] + "\n" + str(projected.get("details") or ""))[:1200],
                    "metadata": projected, "memory_boundary": BOUNDARY})
    return out
