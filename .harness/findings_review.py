"""Read-only, bounded presentation of existing reliability records.

No new ledger or semantic verifier. Analyst wording and machine-checked scopes
are deliberately separate, including when an old run has a passing checkpoint.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import safety

BOUNDARY = (
    "Review of recorded findings only; omitted claims and unknowns are not detected automatically. "
    "MACHINE_CHECKED applies only to checked_scope at capture time, not analyst wording, "
    "dependent conclusions, current source freshness or runtime execution. "
    "Evidence attachment and assessment checkpoint success do not verify an interpretation."
)


def valid_run_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", value)) and ".." not in value


def _claim(row: dict[str, Any]) -> dict[str, Any]:
    receipt = row.get("verifier") or {}
    checked = receipt.get("checked") or {}
    kind = receipt.get("kind")
    scoped = (receipt.get("schema") == 2 and bool(receipt.get("sha256")) and (
        (kind == "code_validate_claim" and bool(checked.get("claim")) and bool(checked.get("source")))
        or (kind == "code_semantics_check" and bool(checked.get("language"))
            and bool(checked.get("operation")) and "inputs" in checked and "observed" in checked)
    ))
    status = row.get("status")
    code_pass = kind == "code_validate_claim" and str(receipt.get("verdict")).upper() == status
    semantics_pass = (kind == "code_semantics_check" and receipt.get("verdict") == "ok"
                      and (checked.get("semantics_class") == "language"
                           or checked.get("toolchain_alignment") == "major_minor_match"))
    label = "UNKNOWN"
    if status == "STALE":
        label = "STALE"
    elif status == "CONFLICT":
        label = "CONTRADICTED"
    elif scoped and (code_pass or semantics_pass):
        if status == "VERIFIED":
            label = "MACHINE_CHECKED"
        elif status == "REFUTED" and code_pass:
            label = "MACHINE_REFUTED"
    return {"id": row.get("claim_id"), "type": "claim", "label": label,
            "analyst_claim": {key: row.get(key) for key in ("subject", "predicate", "value")},
            "interpretation_verified": False, "checked_scope": checked if scoped else None,
            "checked_at": row.get("recorded_at"), "source_freshness": "not_rechecked",
            "required": bool(row.get("required")), "depends_on": row.get("depends_on") or [],
            "reason": row.get("reason") or ("Exact verifier scope unavailable; reverify before relying on it." if not scoped else ""),
            "evidence_ids": row.get("evidence_ids") or []}


def _assessment(row: dict[str, Any]) -> dict[str, Any]:
    refs = [ref.get("evidence_ref") for ref in row.get("evidence_refs") or [] if isinstance(ref, dict)]
    label = "EVIDENCE_ATTACHED_UNVERIFIED" if refs else "UNKNOWN"
    if row.get("kind") in {"gap", "question"} and row.get("status") not in {"resolved", "supported"}:
        label = "UNKNOWN"
    if row.get("status") == "contradicted" or row.get("kind") == "contradiction":
        label = "CONTRADICTED"
    return {"id": row.get("node_id"), "type": "assessment", "label": label,
            "statement": row.get("statement"), "kind": row.get("kind"),
            "recorded_status": row.get("status"), "recorded_authority": row.get("authority"),
            "required": bool(row.get("required")), "evidence_refs": refs,
            "interpretation_verified": False, "evidence_integrity": "not_rechecked",
            "source_freshness": "not_rechecked"}


def review(run: dict[str, Any], *, offset: int = 0, limit: int = 12) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= 50:
        raise ValueError("review offset must be nonnegative and limit must be 1–50")
    rows = [*(_claim(row) for row in run.get("claims") or []),
            *(_assessment(row) for row in run.get("assessments") or [])]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    # Changes to wording, evidence or status change this view identity. A saved
    # report pointer navigates to the current view; it never certifies a note.
    raw = json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str).encode()
    checkpoint = run.get("latest_verification_checkpoint") or {}
    result = {"run_id": run.get("run_id"), "project_id": run.get("project_id"),
              "view": "review", "run_status": run.get("status"), "boundary": BOUNDARY,
              "review_sha256": hashlib.sha256(raw).hexdigest(), "counts": counts,
              "total": len(rows), "offset": offset, "items": rows[offset:offset + limit],
              "checkpoint": {"scope": "graph/evidence requirements, not semantic proof",
                             "recorded_result": checkpoint.get("result", "NOT_RUN"),
                             "stale": bool(run.get("verification_stale"))},
              "risks": run.get("risks") or [], "complete": offset + limit >= len(rows)}
    if not result["complete"]:
        result["next_call"] = {"tool": "reliability_status", "arguments": {
            "run_id": run.get("run_id"), "name": run.get("project_id"),
            "view": "review", "offset": offset + limit, "limit": limit}}
    safe, _ = safety.redact_analysis_nested(result)
    return safe


def memory_links(sources: list[dict[str, Any]], project_id: str) -> list[dict[str, Any]]:
    """Only navigation, including inherited report sources. No file reads."""
    links = []
    for source in sources:
        path = str(source.get("path") or "")
        match = re.fullmatch(r"reports/reliability/([^/]+)\.md", path)
        if source.get("type") == "file" and match and valid_run_id(match[1]):
            link = {"tool": "reliability_status", "arguments": {
                "run_id": match[1], "name": project_id, "view": "review"},
                "claim_verified": False,
                "boundary": "Read the current report. This pointer does not verify this note or its paraphrases."}
            if link not in links:
                links.append(link)
    return links
