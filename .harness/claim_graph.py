from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

ALLOWED_STATUSES = {"VERIFIED", "REFUTED", "INCONCLUSIVE", "STALE", "CONFLICT"}
PASS_CAPABLE = {"VERIFIED", "REFUTED"}


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def verifier_receipt(kind: str, result: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    raw = _canon(payload).encode("utf-8")
    return {
        "kind": str(kind or "deterministic"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "verdict": str(payload.get("verdict") or payload.get("status") or ""),
        "summary": {
            key: payload.get(key)
            for key in (
                "status", "verdict", "reason", "language", "operation", "toolchain",
                "project_toolchain", "repo_id", "source", "observed", "expected",
            )
            if key in payload
        },
    }



def status_from_code_verifier(result: Mapping[str, Any]) -> str:
    verdict = str(result.get("verdict") or "").upper()
    if verdict in {"VERIFIED", "REFUTED"}:
        return verdict
    if str(result.get("status") or "").lower() in {"stale", "stale_source"}:
        return "STALE"
    return "INCONCLUSIVE"


def status_from_semantics_verifier(result: Mapping[str, Any]) -> str:
    if str(result.get("status") or "").lower() != "ok":
        return "INCONCLUSIVE"
    if str(result.get("semantics_class") or "") == "language":
        return "VERIFIED"
    if str(result.get("toolchain_alignment") or "") == "major_minor_match":
        return "VERIFIED"
    return "INCONCLUSIVE"

def normalize_claim(raw: Mapping[str, Any]) -> dict[str, Any]:
    status = str(raw.get("status") or "INCONCLUSIVE").upper()
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"claim status must be one of {sorted(ALLOWED_STATUSES)}")
    claim = {
        "claim_id": str(raw.get("claim_id") or "").strip(),
        "repo_id": str(raw.get("repo_id") or "").strip(),
        "subject": str(raw.get("subject") or "").strip(),
        "predicate": str(raw.get("predicate") or "").strip(),
        "value": raw.get("value"),
        "status": status,
        "required": bool(raw.get("required", True)),
        "evidence_ids": [str(v) for v in (raw.get("evidence_ids") or []) if str(v).strip()],
        "depends_on": [str(v) for v in (raw.get("depends_on") or []) if str(v).strip()],
        "negates": [str(v) for v in (raw.get("negates") or []) if str(v).strip()],
        "reason": str(raw.get("reason") or "").strip(),
        "verifier": dict(raw.get("verifier") or {}),
    }
    if not claim["claim_id"]:
        raise ValueError("claim_id is required")
    if not claim["subject"] or not claim["predicate"]:
        raise ValueError("claim subject and predicate are required")
    if claim["status"] in PASS_CAPABLE and not claim["verifier"].get("sha256"):
        # A model may describe a claim, but it cannot self-certify proof.
        claim["status"] = "INCONCLUSIVE"
        claim["reason"] = claim["reason"] or "VERIFIED/REFUTED requires a deterministic verifier receipt"
    return claim


def contradictions(claims: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [normalize_claim(c) for c in claims]
    by_id = {c["claim_id"]: c for c in normalized}
    conflicts: list[dict[str, Any]] = []
    verified = [c for c in normalized if c["status"] == "VERIFIED"]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for claim in verified:
        groups.setdefault((claim["repo_id"], claim["subject"], claim["predicate"]), []).append(claim)
    for key, rows in groups.items():
        values: dict[str, list[str]] = {}
        for row in rows:
            values.setdefault(_canon(row.get("value")), []).append(row["claim_id"])
        if len(values) > 1:
            conflicts.append({
                "type": "value_conflict",
                "repo_id": key[0], "subject": key[1], "predicate": key[2],
                "claims": [cid for ids in values.values() for cid in ids],
            })
    for claim in verified:
        for other_id in claim.get("negates") or []:
            other = by_id.get(other_id)
            if other and other.get("status") == "VERIFIED":
                pair = sorted([claim["claim_id"], other_id])
                if not any(row.get("type") == "explicit_negation" and row.get("claims") == pair for row in conflicts):
                    conflicts.append({"type": "explicit_negation", "claims": pair})
    return conflicts


def gate(
    claims: list[Mapping[str, Any]],
    *,
    require_claims: bool,
    expected_required_claim_ids: list[str] | None = None,
) -> dict[str, Any]:
    normalized = [normalize_claim(c) for c in claims]
    expected = list(dict.fromkeys(str(v or "").strip() for v in (expected_required_claim_ids or []) if str(v or "").strip()))
    expected_set = set(expected)
    if expected_set:
        for claim in normalized:
            if claim["claim_id"] in expected_set:
                claim["required"] = True
    required = [c for c in normalized if c.get("required")]
    recorded_ids = {c["claim_id"] for c in normalized}
    missing_expected = [claim_id for claim_id in expected if claim_id not in recorded_ids]
    conflicts = contradictions(normalized)
    if missing_expected:
        return {
            "status": "blocked",
            "result": "BLOCKED",
            "reason": "declared required structured claims have not been recorded",
            "missing_required_claims": missing_expected,
            "conflicts": conflicts,
            "claims": normalized,
        }
    if require_claims and not required:
        return {
            "status": "blocked",
            "result": "BLOCKED",
            "reason": "ship mode requires at least one required structured claim",
            "conflicts": [],
            "claims": normalized,
        }
    if not required:
        return {
            "status": "not_applicable",
            "result": "NOT_APPLICABLE",
            "reason": "no required structured claims were declared or recorded",
            "conflicts": conflicts,
            "claims": normalized,
        }
    failed = [c for c in required if c["status"] in {"REFUTED", "CONFLICT"}]
    blocked = [c for c in required if c["status"] in {"INCONCLUSIVE", "STALE"}]
    if conflicts or failed:
        return {
            "status": "failed",
            "result": "CONTRADICTED",
            "reason": "required claims conflict or were refuted",
            "conflicts": conflicts,
            "failed_claims": [c["claim_id"] for c in failed],
            "claims": normalized,
        }
    if blocked:
        return {
            "status": "blocked",
            "result": "BLOCKED",
            "reason": "required claims are not currently machine-verified",
            "conflicts": conflicts,
            "blocked_claims": [c["claim_id"] for c in blocked],
            "claims": normalized,
        }
    return {
        "status": "passed",
        "result": "VERIFIED",
        "reason": "all required structured claims are machine-verified and contradiction-free",
        "conflicts": conflicts,
        "claims": normalized,
    }
