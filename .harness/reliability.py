from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import project_workspace
import acceptance_runs
import claim_graph
import evidence_store
import safety

_ALLOWED_CHECK_STATUS = {"passed", "failed", "blocked", "skipped"}
_ALLOWED_FINAL_STATUS = {"passed", "failed", "blocked", "reliably-paused"}

_ALLOWED_ASSESSMENT_KINDS = {"claim", "hypothesis", "observation", "question", "contradiction", "gap", "decision", "note"}
_ALLOWED_ASSESSMENT_STATUS = {"open", "supported", "contradicted", "resolved", "incomplete", "not_established"}
_ALLOWED_AUTHORITIES = {
    "tool_evidence", "source_evidence", "user_supplied_evidence", "environment_observation",
    "analyst_observation", "model_inference", "external_reference", "legacy_observation", "runtime_observation",
}
_ALLOWED_RELATIONS = {"supports", "refutes", "derived_from", "conflicts_with", "requires", "answers", "motivates_followup"}
_MAX_ASSESSMENTS = 128
_MAX_CHECKPOINTS = 32
_MAX_EVIDENCE_REFS = 16
_MAX_STATEMENT = 2400
_MAX_SUMMARY = 3200
_MAX_RELATIONS = 32
_MAX_RELATION_RECORDS = 512
_DEFAULT_CORRECTIVE_BUDGET = 1
_PASSING_CHECKPOINT_RESULTS = {"VERIFIED", "VERIFIED_WITH_FINDINGS", "clear"}
_ALLOWED_REQUIREMENTS = {"reranker_complete", "single_evidence_scope"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "reliability").strip()).strip("-._")
    return clean[:80] or "reliability"


def _bounded_text(value: Any, limit: int, *, max_newlines: int = 12) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError(f"text exceeds {limit} characters")
    if text.count("\n") > max_newlines:
        raise ValueError("text is too multiline for compact reliability state")
    redacted, _ = safety.redact_text(text)
    return redacted


def _clean_node_id(value: str, prefix: str = "asn_") -> str:
    clean = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "").strip()).strip("-._:")
    return clean[:160] if clean else prefix + uuid.uuid4().hex[:16]


def _clean_relation_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._:-]+", "-", str(value or "").strip()).strip("-._:")
    return clean[:160]


def _legacy_relation_id(from_node_id: str, relation_type: str, to_node_id: str) -> str:
    raw = f"{from_node_id}\0{relation_type}\0{to_node_id}".encode("utf-8")
    return "rel_" + hashlib.sha256(raw).hexdigest()[:20]


def _normalize_relations(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Return canonical first-class relations, migrating embedded v2 edges."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in run.get("relations") or []:
        if not isinstance(raw, dict):
            continue
        from_id = str(raw.get("from_node_id") or "").strip()
        to_id = str(raw.get("to_node_id") or raw.get("target_id") or "").strip()
        rel_type = str(raw.get("type") or "").strip().lower()
        if not from_id or not to_id or rel_type not in _ALLOWED_RELATIONS:
            continue
        rel_id = _clean_relation_id(str(raw.get("relation_id") or "")) or _legacy_relation_id(from_id, rel_type, to_id)
        if rel_id in seen:
            continue
        seen.add(rel_id)
        records.append({
            "relation_id": rel_id,
            "from_node_id": from_id,
            "type": rel_type,
            "to_node_id": to_id,
            "recorded_at": str(raw.get("recorded_at") or ""),
        })
    for node in run.get("assessments") or []:
        if not isinstance(node, dict):
            continue
        from_id = str(node.get("node_id") or "").strip()
        for raw in node.get("relations") or []:
            if not isinstance(raw, dict):
                continue
            to_id = str(raw.get("to_node_id") or raw.get("target_id") or "").strip()
            rel_type = str(raw.get("type") or "").strip().lower()
            if not from_id or not to_id or rel_type not in _ALLOWED_RELATIONS:
                continue
            rel_id = _clean_relation_id(str(raw.get("relation_id") or "")) or _legacy_relation_id(from_id, rel_type, to_id)
            if rel_id in seen:
                continue
            seen.add(rel_id)
            records.append({
                "relation_id": rel_id,
                "from_node_id": from_id,
                "type": rel_type,
                "to_node_id": to_id,
                "recorded_at": str(node.get("recorded_at") or ""),
            })
    return records[:_MAX_RELATION_RECORDS]


def _relation_targets(relations: list[dict[str, Any]], node_id: str, allowed_types: set[str]) -> list[str]:
    return [
        str(row.get("to_node_id") or "")
        for row in relations
        if str(row.get("from_node_id") or "") == node_id and str(row.get("type") or "") in allowed_types
    ]


def _subject_contract(run: dict[str, Any]) -> dict[str, Any]:
    contract = dict(run.get("subject_contract") or {})
    required_claims = [str(value or "").strip() for value in contract.get("required_claims") or [] if str(value or "").strip()]
    required_properties = [str(value or "").strip() for value in contract.get("required_properties") or [] if str(value or "").strip()]
    return {
        "subject": str(contract.get("subject") or run.get("subject") or ""),
        "required_claims": list(dict.fromkeys(required_claims))[:128],
        "required_properties": list(dict.fromkeys(required_properties))[:128],
    }


def _claim_gate(run: dict[str, Any], *, require_claims: bool | None = None) -> dict[str, Any]:
    contract = _subject_contract(run)
    return claim_graph.gate(
        list(run.get("claims") or []),
        require_claims=(str(run.get("mode") or "") == "ship") if require_claims is None else bool(require_claims),
        expected_required_claim_ids=contract["required_claims"],
    )


def _budget_state(run: dict[str, Any]) -> dict[str, Any]:
    raw = dict(run.get("corrective_budget") or {})
    try:
        total = max(0, min(int(raw.get("total") if "total" in raw else _DEFAULT_CORRECTIVE_BUDGET), 8))
    except (TypeError, ValueError):
        total = _DEFAULT_CORRECTIVE_BUDGET
    try:
        used = max(0, min(int(raw.get("used") or 0), total))
    except (TypeError, ValueError):
        used = 0
    events = [dict(row) for row in raw.get("events") or [] if isinstance(row, dict)][-16:]
    return {"total": total, "used": used, "remaining": max(0, total - used), "events": events}


def _evidence_meta(root: Path, project_id: str, refs: list[str] | None) -> tuple[list[dict[str, Any]], list[str]]:
    resolved: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for raw in (refs or [])[:_MAX_EVIDENCE_REFS]:
        ref = str(raw or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        meta = evidence_store.metadata(root, project_id, ref)
        if meta.get("status") != "ok":
            errors.append(f"evidence unavailable or failed integrity validation: {ref}")
            continue
        resolved.append({
            "evidence_ref": ref,
            "kind": meta.get("kind"),
            "tool": meta.get("tool"),
            "artifact_sha256": meta.get("artifact_sha256"),
            "payload_sha256": meta.get("payload_sha256"),
            "scope_identity": meta.get("scope_identity") or {},
            "backend_observations": meta.get("backend_observations") or {},
        })
    return resolved, errors


def _project_id(root: Path, name: str = "", session_id: str = "") -> str:
    if name.strip():
        project_id = project_workspace.clean_project_id(name)
    else:
        project_id = project_workspace.current_project_id(root, session_id=session_id) or ""
    if not project_id or not project_workspace.project_exists(root, project_id):
        raise ValueError("No active project. Open a project or provide name= explicitly.")
    return project_id


def _report_dir(root: Path, project_id: str) -> Path:
    return project_workspace.paths_for(root, project_id).project_dir / "reports" / "reliability"


def _json_path(root: Path, project_id: str, run_id: str) -> Path:
    return _report_dir(root, project_id) / f"{run_id}.json"


def _markdown_path(root: Path, project_id: str, run_id: str) -> Path:
    return _report_dir(root, project_id) / f"{run_id}.md"


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


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Unknown reliability run: {path.stem}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid reliability run state: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Invalid reliability run state: {path}")
    return data


def _render_markdown(run: dict[str, Any]) -> str:
    lines = [
        f"# Reliability report: {run.get('subject') or run.get('run_id')}",
        "",
        f"- Run ID: `{run.get('run_id')}`",
        f"- Project: `{run.get('project_id')}`",
        f"- Mode: `{run.get('mode')}`",
        f"- Status: **{run.get('status')}**",
        f"- Started: {run.get('started_at')}",
        f"- Updated: {run.get('updated_at')}",
        f"- Subject: {(_subject_contract(run)).get('subject') or 'not specified'}",
        f"- Required claims: {', '.join((_subject_contract(run)).get('required_claims') or []) or 'none'}",
        f"- Required properties: {', '.join((_subject_contract(run)).get('required_properties') or []) or 'none'}",
        f"- Corrective budget: {_budget_state(run)['used']}/{_budget_state(run)['total']} used ({_budget_state(run)['remaining']} remaining)",
        "",
        "## Claim",
        "",
        str(run.get("claim") or "No claim recorded."),
        "",
        "## Checks",
        "",
    ]
    checks = run.get("checks") or []
    if not checks:
        lines.append("No checks recorded.")
    for check in checks:
        marker = {"passed": "x", "failed": "!", "blocked": "?", "skipped": "-"}.get(str(check.get("status")), " ")
        required = "required" if check.get("required") else "optional"
        lines.extend([
            f"- [{marker}] **{check.get('name')}** — `{check.get('status')}` ({required})",
            f"  - Command/action: `{check.get('command') or 'not recorded'}`",
            f"  - Evidence: {check.get('evidence') or 'not recorded'}",
        ])
    lines.extend(["", "## Structured claims", ""])
    claims = run.get("claims") or []
    if not claims:
        lines.append("No structured claims recorded.")
    for claim in claims:
        lines.append(f"- `{claim.get('claim_id')}` **{claim.get('status')}** {claim.get('repo_id') or '(project)'} :: {claim.get('subject')} / {claim.get('predicate')} = `{json.dumps(claim.get('value'), ensure_ascii=False, sort_keys=True, default=str)}`")
        if claim.get("reason"):
            lines.append(f"  - {claim.get('reason')}")
    gate = run.get("claim_gate") or {}
    if gate:
        lines.append(f"- Claim gate: **{gate.get('status')}** — {gate.get('reason')}")
    lines.extend(["", "## Assessment graph", ""])
    assessments = run.get("assessments") or []
    if not assessments:
        lines.append("No assessment nodes recorded.")
    for node in assessments:
        lines.append(f"- `{node.get('node_id')}` **{node.get('kind')} / {node.get('status')}** [{node.get('authority')}] {node.get('statement')}")
        refs = [str((ref or {}).get("evidence_ref") or "") for ref in (node.get("evidence_refs") or []) if isinstance(ref, dict)]
        if refs:
            lines.append("  - Evidence refs: " + ", ".join(f"`{ref}`" for ref in refs))
        if node.get("analysis_summary"):
            lines.append("  - Summary: " + str(node.get("analysis_summary")))
    relations = _normalize_relations(run)
    if relations:
        lines.extend(["", "## Relations", ""])
        for relation in relations:
            lines.append(
                f"- `{relation.get('relation_id')}` `{relation.get('from_node_id')}` --{relation.get('type')}--> `{relation.get('to_node_id')}`"
            )
    latest_checkpoint = run.get("latest_verification_checkpoint") or {}
    if latest_checkpoint:
        lines.extend(["", "## Verification checkpoint", "", f"- Result: **{latest_checkpoint.get('result')}**", f"- Checkpoint: `{latest_checkpoint.get('checkpoint_id')}` iteration {latest_checkpoint.get('iteration')}"] )
        for finding in latest_checkpoint.get("backend_reliability_findings") or []:
            lines.append(f"- Backend finding: `{finding.get('backend')}` / `{finding.get('failure_class')}` — {finding.get('reason')}")
    lines.extend(["", "## Unresolved risk and untested paths", ""])
    risks = run.get("risks") or []
    lines.extend([f"- {item}" for item in risks] or ["- None recorded."])
    lines.extend(["", "## Gate decision", "", str(run.get("decision_reason") or "Not finalized."), ""])
    return "\n".join(lines)


def _write_all(root: Path, run: dict[str, Any]) -> dict[str, Any]:
    project_id = str(run["project_id"])
    run_id = str(run["run_id"])
    json_path = _json_path(root, project_id, run_id)
    md_path = _markdown_path(root, project_id, run_id)
    _atomic_json(json_path, run)
    md_path.write_text(_render_markdown(run), encoding="utf-8")
    result = dict(run)
    pp = project_workspace.paths_for(root, project_id)
    result["json_path"] = str(json_path.relative_to(pp.project_dir))
    result["report_path"] = str(md_path.relative_to(pp.project_dir))
    return result


def start_run(
    root: Path,
    *,
    claim: str,
    required_checks: list[str],
    subject: str = "",
    mode: str = "reliability",
    required_claims: list[str] | None = None,
    required_properties: list[str] | None = None,
    corrective_budget: int = _DEFAULT_CORRECTIVE_BUDGET,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    contract_claims = list(dict.fromkeys(
        str(value or "").strip() for value in (required_claims or []) if str(value or "").strip()
    ))[:128]
    contract_properties = list(dict.fromkeys(
        _bounded_text(value, 600, max_newlines=2) for value in (required_properties or []) if str(value or "").strip()
    ))[:128]
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Required properties remain free-form, but become normal required checks so
    # declaring one creates an actual finalization obligation rather than metadata.
    for raw in [*(required_checks or []), *contract_properties]:
        check_name = str(raw).strip()
        if not check_name or check_name in seen:
            continue
        seen.add(check_name)
        checks.append({
            "name": check_name,
            "required": True,
            "status": "blocked",
            "command": "",
            "evidence": "Not run yet.",
            "recorded_at": "",
        })
    if not checks:
        raise ValueError("required_checks or required_properties must contain at least one concrete verification obligation")
    try:
        budget_total = max(0, min(int(corrective_budget), 8))
    except (TypeError, ValueError) as exc:
        raise ValueError("corrective_budget must be an integer from 0 to 8") from exc
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{timestamp}-{_slug(subject or claim)[:40]}-{uuid.uuid4().hex[:8]}"
    now = _now()
    clean_subject = subject.strip() or _slug(claim).replace("-", " ")
    run = {
        "schema_version": 3,
        "run_id": run_id,
        "project_id": project_id,
        "mode": mode if mode in {"verify", "reliability", "ship"} else "reliability",
        "subject": clean_subject,
        "subject_contract": {
            "subject": clean_subject,
            "required_claims": contract_claims,
            "required_properties": contract_properties,
        },
        "claim": claim.strip(),
        "status": "in_progress",
        "checks": checks,
        "claims": [],
        "claim_gate": {},
        "assessments": [],
        "relations": [],
        "corrective_budget": {"total": budget_total, "used": 0, "remaining": budget_total, "events": []},
        "verification_checkpoints": [],
        "latest_verification_checkpoint": {},
        "verification_stale": False,
        "risks": [],
        "started_at": now,
        "updated_at": now,
        "finalized_at": "",
        "decision_reason": "Required checks have not all passed.",
    }
    return _write_all(root, run)

def record_check(
    root: Path,
    *,
    run_id: str,
    check_name: str,
    status: str,
    command: str = "",
    evidence: str = "",
    required: bool = True,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    normalized = status.strip().lower()
    if normalized not in _ALLOWED_CHECK_STATUS:
        raise ValueError(f"status must be one of {sorted(_ALLOWED_CHECK_STATUS)}")
    with _lock(state_path):
        run = _read(state_path)
        if run.get("status") != "in_progress":
            raise ValueError("reliability run is already finalized")
        checks = list(run.get("checks") or [])
        target = None
        for check in checks:
            if str(check.get("name")) == check_name:
                target = check
                break
        if target is None:
            target = {"name": check_name, "required": bool(required)}
            checks.append(target)
        target.update({
            "required": bool(required),
            "status": normalized,
            "command": command.strip(),
            "evidence": evidence.strip(),
            "recorded_at": _now(),
        })
        run["checks"] = checks
        run["updated_at"] = _now()
        return _write_all(root, run)


def record_claim(
    root: Path,
    *,
    run_id: str,
    claim_id: str,
    subject: str,
    predicate: str,
    value: Any,
    status: str = "INCONCLUSIVE",
    repo_id: str = "",
    required: bool = True,
    evidence_ids: list[str] | None = None,
    depends_on: list[str] | None = None,
    negates: list[str] | None = None,
    reason: str = "",
    verifier: dict[str, Any] | None = None,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    raw = {
        "claim_id": claim_id, "repo_id": repo_id, "subject": subject, "predicate": predicate,
        "value": value, "status": status, "required": required, "evidence_ids": evidence_ids or [],
        "depends_on": depends_on or [], "negates": negates or [], "reason": reason, "verifier": verifier or {},
    }
    normalized = claim_graph.normalize_claim(raw)
    normalized["recorded_at"] = _now()
    with _lock(state_path):
        run = _read(state_path)
        if run.get("status") != "in_progress":
            raise ValueError("reliability run is already finalized")
        if normalized["claim_id"] in set(_subject_contract(run)["required_claims"]):
            normalized["required"] = True
        claims = [dict(c) for c in (run.get("claims") or []) if str(c.get("claim_id")) != normalized["claim_id"]]
        claims.append(normalized)
        run["claims"] = claims
        run["claim_gate"] = _claim_gate(run, require_claims=False)
        run["verification_stale"] = True
        run["updated_at"] = _now()
        return _write_all(root, run)


def record_assessment(
    root: Path,
    *,
    run_id: str,
    kind: str,
    statement: str,
    node_id: str = "",
    status: str = "open",
    authority: str = "model_inference",
    analysis_summary: str = "",
    evidence_refs: list[str] | None = None,
    relations: list[dict[str, str]] | None = None,
    required: bool = False,
    confidence: str = "provisional",
    requirements: list[str] | None = None,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Record concise analyst state without persisting private reasoning.

    Semantic content remains expressive. Identity, authority, evidence provenance,
    relations, and lifecycle are the strict boundary. Embedded relations are kept
    as a compatibility input but are persisted canonically as first-class records.
    """
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    clean_kind = str(kind or "").strip().lower()
    clean_status = str(status or "open").strip().lower()
    clean_authority = str(authority or "model_inference").strip().lower()
    if clean_kind not in _ALLOWED_ASSESSMENT_KINDS:
        raise ValueError(f"kind must be one of {sorted(_ALLOWED_ASSESSMENT_KINDS)}")
    if clean_status not in _ALLOWED_ASSESSMENT_STATUS:
        raise ValueError(f"status must be one of {sorted(_ALLOWED_ASSESSMENT_STATUS)}")
    if clean_authority not in _ALLOWED_AUTHORITIES:
        raise ValueError(f"authority must be one of {sorted(_ALLOWED_AUTHORITIES)}")
    if clean_kind == "note" and required:
        raise ValueError("note nodes are deliberately non-gating; promote important note content into a claim, observation, question, gap, or decision")
    clean_statement = _bounded_text(statement, _MAX_STATEMENT, max_newlines=8)
    if not clean_statement:
        raise ValueError("statement is required")
    clean_summary = _bounded_text(analysis_summary, _MAX_SUMMARY, max_newlines=16) if analysis_summary else ""
    clean_confidence = re.sub(r"[^a-z0-9_-]+", "-", str(confidence or "provisional").strip().lower())[:40] or "provisional"
    clean_requirements: list[str] = []
    for raw_requirement in requirements or []:
        requirement = str(raw_requirement or "").strip().lower()
        if requirement not in _ALLOWED_REQUIREMENTS:
            raise ValueError(f"requirement must be one of {sorted(_ALLOWED_REQUIREMENTS)}")
        if requirement not in clean_requirements:
            clean_requirements.append(requirement)
    clean_id = _clean_node_id(node_id)
    with _lock(state_path):
        run = _read(state_path)
        if run.get("status") != "in_progress":
            raise ValueError("reliability run is already finalized")
        nodes = [dict(row) for row in (run.get("assessments") or [])]
        if len(nodes) >= _MAX_ASSESSMENTS and all(str(row.get("node_id")) != clean_id for row in nodes):
            raise ValueError("reliability assessment node limit reached")
        existing_ids = {str(row.get("node_id") or "") for row in nodes}
        existing_ids.add(clean_id)
        canonical_relations = _normalize_relations(run)
        relation_ids: list[str] = []
        for relation in (relations or [])[:_MAX_RELATIONS]:
            if not isinstance(relation, dict):
                raise ValueError("relations must be objects with type and target_id")
            rel_type = str(relation.get("type") or "").strip().lower()
            target_id = str(relation.get("target_id") or relation.get("to_node_id") or "").strip()
            if rel_type not in _ALLOWED_RELATIONS:
                raise ValueError(f"relation type must be one of {sorted(_ALLOWED_RELATIONS)}")
            if not target_id or target_id not in existing_ids:
                raise ValueError(f"relation target does not exist in this reliability run: {target_id}")
            rel_id = _clean_relation_id(str(relation.get("relation_id") or "")) or _legacy_relation_id(clean_id, rel_type, target_id)
            canonical = {
                "relation_id": rel_id,
                "from_node_id": clean_id,
                "type": rel_type,
                "to_node_id": target_id,
                "recorded_at": _now(),
            }
            canonical_relations = [row for row in canonical_relations if str(row.get("relation_id") or "") != rel_id]
            canonical_relations.append(canonical)
            relation_ids.append(rel_id)
        resolved_refs, ref_errors = _evidence_meta(root, project_id, evidence_refs)
        if ref_errors:
            raise ValueError("; ".join(ref_errors))
        node = {
            "node_id": clean_id,
            "kind": clean_kind,
            "statement": clean_statement,
            "status": clean_status,
            "authority": clean_authority,
            "analysis_summary": clean_summary,
            "evidence_refs": resolved_refs,
            "relations": [],
            "relation_ids": relation_ids,
            "required": bool(required),
            "confidence": clean_confidence,
            "requirements": clean_requirements,
            "recorded_at": _now(),
        }
        nodes = [row for row in nodes if str(row.get("node_id") or "") != clean_id]
        nodes.append(node)
        run["schema_version"] = max(3, int(run.get("schema_version") or 1))
        run["assessments"] = nodes
        run["relations"] = canonical_relations[:_MAX_RELATION_RECORDS]
        run["verification_stale"] = True
        run["updated_at"] = _now()
        saved = _write_all(root, run)
    return {"status": "recorded", "run_id": run_id, "project_id": project_id, "assessment": node, "run": saved}


def record_relation(
    root: Path,
    *,
    run_id: str,
    from_node_id: str,
    relation_type: str,
    to_node_id: str,
    relation_id: str = "",
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    raw_from = str(from_node_id or "").strip()
    raw_to = str(to_node_id or "").strip()
    if not raw_from or not raw_to:
        raise ValueError("from_node_id and to_node_id are required")
    from_id = _clean_node_id(raw_from)
    to_id = _clean_node_id(raw_to)
    rel_type = str(relation_type or "").strip().lower()
    if rel_type not in _ALLOWED_RELATIONS:
        raise ValueError(f"relation type must be one of {sorted(_ALLOWED_RELATIONS)}")
    rel_id = _clean_relation_id(relation_id) or _legacy_relation_id(from_id, rel_type, to_id)
    with _lock(state_path):
        run = _read(state_path)
        if run.get("status") != "in_progress":
            raise ValueError("reliability run is already finalized")
        node_ids = {str(row.get("node_id") or "") for row in run.get("assessments") or [] if isinstance(row, dict)}
        missing = [node_id for node_id in (from_id, to_id) if node_id not in node_ids]
        if missing:
            raise ValueError("relation endpoint does not exist in this reliability run: " + ", ".join(missing))
        records = _normalize_relations(run)
        relation = {
            "relation_id": rel_id,
            "from_node_id": from_id,
            "type": rel_type,
            "to_node_id": to_id,
            "recorded_at": _now(),
        }
        records = [row for row in records if str(row.get("relation_id") or "") != rel_id]
        records.append(relation)
        if len(records) > _MAX_RELATION_RECORDS:
            raise ValueError("reliability relation record limit reached")
        run["schema_version"] = max(3, int(run.get("schema_version") or 1))
        run["relations"] = records
        run["verification_stale"] = True
        run["updated_at"] = _now()
        _write_all(root, run)
    return {"status": "recorded", "run_id": run_id, "project_id": project_id, "relation": relation}


def consume_corrective_budget(
    root: Path,
    *,
    run_id: str,
    action: str,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    clean_action = _bounded_text(action, 1000, max_newlines=3)
    if not clean_action:
        raise ValueError("action is required")
    with _lock(state_path):
        run = _read(state_path)
        if run.get("status") != "in_progress":
            raise ValueError("reliability run is already finalized")
        budget = _budget_state(run)
        if budget["remaining"] <= 0:
            return {
                "status": "rejected",
                "reason": "corrective_budget_exhausted",
                "run_id": run_id,
                "project_id": project_id,
                "corrective_budget": budget,
            }
        event = {"event_id": "cor_" + uuid.uuid4().hex[:16], "action": clean_action, "consumed_at": _now()}
        budget["used"] += 1
        budget["remaining"] = max(0, budget["total"] - budget["used"])
        budget["events"] = (budget["events"] + [event])[-16:]
        run["corrective_budget"] = budget
        run["updated_at"] = _now()
        _write_all(root, run)
    return {"status": "consumed", "run_id": run_id, "project_id": project_id, "event": event, "corrective_budget": budget}

def verification_checkpoint(
    root: Path,
    *,
    run_id: str,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Run deterministic coherence/provenance checks over flexible analyst state.

    Results separate proven mechanics from findings, incompleteness, contradiction,
    broken provenance, and a genuinely non-applicable structured-claim gate.
    """
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    with _lock(state_path):
        run = _read(state_path)
        if run.get("status") != "in_progress":
            raise ValueError("reliability run is already finalized")
        nodes = [dict(row) for row in (run.get("assessments") or [])]
        by_id = {str(row.get("node_id") or ""): row for row in nodes if str(row.get("node_id") or "")}
        relations = _normalize_relations(run)
        missing_refs: list[str] = []
        broken_relations: list[dict[str, Any]] = []
        scope_identities: dict[str, list[str]] = {}
        backend_findings: list[dict[str, Any]] = []
        required_degraded_nodes: list[str] = []
        scope_requirement_violations: list[str] = []
        evidence_backed_nodes: set[str] = set()

        for relation in relations:
            from_id = str(relation.get("from_node_id") or "")
            to_id = str(relation.get("to_node_id") or "")
            missing_endpoints = [node_id for node_id in (from_id, to_id) if node_id not in by_id]
            if missing_endpoints:
                broken_relations.append({
                    "relation_id": str(relation.get("relation_id") or ""),
                    "from_node_id": from_id,
                    "to_node_id": to_id,
                    "missing_node_ids": missing_endpoints,
                })

        for node in nodes:
            node_id = str(node.get("node_id") or "")
            node_scopes: set[str] = set()
            for ref_meta in node.get("evidence_refs") or []:
                ref = str((ref_meta or {}).get("evidence_ref") or "") if isinstance(ref_meta, dict) else ""
                if not ref:
                    continue
                meta = evidence_store.metadata(root, project_id, ref)
                if meta.get("status") != "ok":
                    missing_refs.append(ref)
                    continue
                evidence_backed_nodes.add(node_id)
                scope = json.dumps(meta.get("scope_identity") or {}, sort_keys=True, separators=(",", ":"))
                scope_identities.setdefault(scope, []).append(ref)
                node_scopes.add(scope)
                reranker = dict((meta.get("backend_observations") or {}).get("reranker") or {})
                if reranker.get("attempted") and not reranker.get("applied"):
                    finding = {
                        "node_id": node_id,
                        "evidence_ref": ref,
                        "backend": "reranker",
                        "failure_class": reranker.get("failure_class") or "unknown",
                        "retryable": bool(reranker.get("retryable")),
                        "reason": str(reranker.get("reason") or "")[:300],
                    }
                    backend_findings.append(finding)
                    if bool(node.get("required")) and "reranker_complete" in set(node.get("requirements") or []):
                        required_degraded_nodes.append(node_id)
            if bool(node.get("required")) and "single_evidence_scope" in set(node.get("requirements") or []) and len(node_scopes) > 1:
                scope_requirement_violations.append(node_id)

        required_unresolved: list[str] = []
        unresolved_contradictions: list[str] = []
        required_contradictions: list[str] = []
        open_gaps: list[str] = []
        next_check_candidates: list[str] = []
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            kind = str(node.get("kind") or "")
            status = str(node.get("status") or "")
            authority = str(node.get("authority") or "")
            required = bool(node.get("required"))
            if kind == "note":
                continue
            if kind == "contradiction" and status != "resolved":
                unresolved_contradictions.append(node_id)
                if required:
                    required_contradictions.append(node_id)
            if kind in {"gap", "question"} and status != "resolved":
                open_gaps.append(node_id)
                if len(next_check_candidates) < 8:
                    next_check_candidates.append(str(node.get("statement") or "")[:500])
            if not required:
                continue
            if kind in {"gap", "question", "contradiction"} and status != "resolved":
                required_unresolved.append(node_id)
                continue
            if kind in {"claim", "decision", "observation"} and status not in {"supported", "resolved"}:
                required_unresolved.append(node_id)
                continue
            if kind in {"claim", "decision", "observation"} and status in {"supported", "resolved"}:
                supporting_nodes = set(_relation_targets(relations, node_id, {"derived_from"}))
                supporting_nodes.update(
                    str(row.get("from_node_id") or "")
                    for row in relations
                    if str(row.get("to_node_id") or "") == node_id and str(row.get("type") or "") == "supports"
                )
                relation_has_evidence = any(target in evidence_backed_nodes for target in supporting_nodes)
                if authority in {"tool_evidence", "source_evidence", "environment_observation", "runtime_observation", "external_reference"}:
                    if node_id not in evidence_backed_nodes:
                        required_unresolved.append(node_id)
                elif authority == "model_inference":
                    if node_id not in evidence_backed_nodes and not relation_has_evidence:
                        required_unresolved.append(node_id)

        claim_gate = _claim_gate(run)
        multiple_evidence_scopes = len(scope_identities) > 1
        has_findings = bool(unresolved_contradictions or open_gaps or backend_findings or multiple_evidence_scopes)
        if required_contradictions or claim_gate.get("status") == "failed":
            result = "CONTRADICTED"
        elif missing_refs or broken_relations:
            result = "BLOCKED"
        elif (
            required_unresolved
            or required_degraded_nodes
            or scope_requirement_violations
            or claim_gate.get("status") == "blocked"
        ):
            result = "INCOMPLETE"
        elif not nodes and claim_gate.get("status") == "not_applicable":
            result = "NOT_APPLICABLE"
        elif has_findings:
            result = "VERIFIED_WITH_FINDINGS"
        else:
            result = "VERIFIED"

        previous = list(run.get("verification_checkpoints") or [])
        iteration = (int(previous[-1].get("iteration") or 0) + 1) if previous else 1
        budget = _budget_state(run)
        checkpoint = {
            "checkpoint_id": "vrf_" + uuid.uuid4().hex[:16],
            "iteration": iteration,
            "result": result,
            "missing_evidence_refs": sorted(set(missing_refs)),
            "broken_relations": broken_relations[:32],
            "relation_count": len(relations),
            "multiple_evidence_scopes": multiple_evidence_scopes,
            "scope_requirement_violations": sorted(set(scope_requirement_violations)),
            "required_unresolved_nodes": sorted(set(required_unresolved)),
            "unresolved_contradictions": sorted(set(unresolved_contradictions)),
            "required_contradictions": sorted(set(required_contradictions)),
            "open_gap_or_question_nodes": sorted(set(open_gaps)),
            "backend_reliability_findings": backend_findings[:32],
            "required_nodes_with_degraded_backend_evidence": sorted(set(required_degraded_nodes)),
            "claim_gate": claim_gate,
            "next_check_candidates": next_check_candidates,
            "corrective_budget_total": budget["total"],
            "corrective_budget_used": budget["used"],
            "corrective_budget_remaining": budget["remaining"],
            "interpretive_boundary": "This checkpoint validates provenance, first-class graph coherence, deterministic claim receipts, and backend evidence health. Declared required properties are materialized as ordinary required checks and must pass before finalization; it does not self-certify property text or model inference as proof.",
            "recorded_at": _now(),
        }
        previous.append(checkpoint)
        run["schema_version"] = max(3, int(run.get("schema_version") or 1))
        run["relations"] = relations
        run["corrective_budget"] = budget
        run["claim_gate"] = claim_gate
        run["verification_checkpoints"] = previous[-_MAX_CHECKPOINTS:]
        run["latest_verification_checkpoint"] = checkpoint
        run["verification_stale"] = False
        run["updated_at"] = _now()
        _write_all(root, run)
    return {"status": "ok", "run_id": run_id, "project_id": project_id, **checkpoint}

def finish_run(
    root: Path,
    *,
    run_id: str,
    requested_status: str = "passed",
    risks: list[str] | None = None,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    state_path = _json_path(root, project_id, run_id)
    requested = requested_status.strip().lower()
    if requested not in _ALLOWED_FINAL_STATUS:
        raise ValueError(f"requested_status must be one of {sorted(_ALLOWED_FINAL_STATUS)}")
    with _lock(state_path):
        run = _read(state_path)
        checks = list(run.get("checks") or [])
        required = [c for c in checks if c.get("required")]
        failed = [c for c in required if c.get("status") == "failed"]
        missing = [c for c in required if c.get("status") in {None, "", "blocked", "skipped"}]
        claim_gate = _claim_gate(run)
        run["claim_gate"] = claim_gate
        required_assessments = [row for row in (run.get("assessments") or []) if bool((row or {}).get("required"))]
        verification = dict(run.get("latest_verification_checkpoint") or {})
        verification_stale = bool(run.get("verification_stale"))
        if required_assessments and (verification_stale or not verification):
            final = "blocked" if requested != "reliably-paused" else "reliably-paused"
            reason = "Required assessment state has not passed a current reliability_verification_checkpoint."
        elif required_assessments and verification.get("result") in {"CONTRADICTED", "contradicted"}:
            final = "failed"
            reason = "Required assessment verification found unresolved contradiction or failed deterministic claim evidence."
        elif required_assessments and verification.get("result") not in _PASSING_CHECKPOINT_RESULTS:
            final = "blocked" if requested != "reliably-paused" else "reliably-paused"
            reason = "Required assessment verification is incomplete or blocked; resolve missing/degraded evidence or required gaps before passing."
        elif claim_gate.get("status") == "failed":
            final = "failed"
            reason = "Structured claim gate failed: " + str(claim_gate.get("reason") or "claim conflict/refutation")
        elif claim_gate.get("status") == "blocked" and str(run.get("mode") or "") == "ship":
            final = "blocked" if requested != "reliably-paused" else "reliably-paused"
            reason = "Structured claim gate blocked shipping: " + str(claim_gate.get("reason") or "claims not verified")
        elif failed:
            final = "failed"
            reason = "One or more required checks failed: " + ", ".join(str(c.get("name")) for c in failed)
        elif missing:
            final = "blocked" if requested != "reliably-paused" else "reliably-paused"
            reason = "Required checks were not observed to pass: " + ", ".join(str(c.get("name")) for c in missing)
        elif requested in {"failed", "blocked", "reliably-paused"}:
            final = requested
            reason = f"Caller finalized the fully observed run as {requested}."
        else:
            final = "passed"
            reason = "Every required check was observed to pass."
        run["status"] = final
        run["requested_status"] = requested
        run["risks"] = [str(item).strip() for item in (risks or []) if str(item).strip()]
        run["decision_reason"] = reason
        run["updated_at"] = _now()
        run["finalized_at"] = _now()
        saved = _write_all(root, run)
    try:
        project_workspace.project_capture(
            root,
            project_id,
            summary=f"Reliability run `{run_id}` finalized as {saved['status']}.",
            kind="artifact",
            details=reason,
            sources=[{"type": "file", "path": saved["report_path"]}],
            confidence="high",
            metadata={"reliability_run_id": run_id, "reliability_status": saved["status"]},
            refresh=True,
        )
    except Exception:
        # The deterministic gate result is already durable. Continuity capture is best effort.
        saved["continuity_capture_warning"] = "Reliability report saved, but continuity capture failed."
    return saved


def aggregate_verdict(
    root: Path,
    *,
    reliability_run_id: str = "",
    acceptance_run_id: str = "",
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Compose component ledgers without letting one passing sub-suite mask another."""
    project_id = _project_id(root, name=name, session_id=session_id)
    components: dict[str, dict[str, Any]] = {}

    def reliability_verdict(run: dict[str, Any]) -> str:
        status = str(run.get("status") or "").lower()
        if status == "passed":
            return "PASSED"
        if status == "failed":
            return "NOT_PASSED"
        if status in {"blocked", "reliably-paused"}:
            return "BLOCKED"
        if status == "in_progress":
            return "INCOMPLETE"
        return "NOT_APPLICABLE"

    def acceptance_verdict(run: dict[str, Any]) -> str:
        if not run or run.get("status") == "none":
            return "NOT_APPLICABLE"
        if run.get("status") == "migration_blocked":
            return "BLOCKED"
        run_status = str(run.get("run_status") or run.get("status") or "").lower()
        if run_status == "running":
            return "INCOMPLETE"
        outcome = str(run.get("ledger_outcome") or "").lower()
        if outcome == "complete":
            return "PASSED"
        if outcome == "not_passed":
            return "NOT_PASSED"
        return "INCOMPLETE"

    if reliability_run_id:
        run = _read(_json_path(root, project_id, reliability_run_id))
        components["reliability"] = {
            "run_id": reliability_run_id,
            "verdict": reliability_verdict(run),
            "status": run.get("status"),
            "checkpoint_result": (run.get("latest_verification_checkpoint") or {}).get("result"),
        }
    if acceptance_run_id:
        run = acceptance_runs.status(root, run_id=acceptance_run_id, project_id=project_id)
        components["acceptance"] = {
            "run_id": acceptance_run_id,
            "verdict": acceptance_verdict(run),
            "run_status": run.get("run_status"),
            "ledger_outcome": run.get("ledger_outcome"),
        }
    if not components:
        raise ValueError("provide reliability_run_id and/or acceptance_run_id")
    verdicts = [row["verdict"] for row in components.values()]
    if "NOT_PASSED" in verdicts:
        overall = "NOT_PASSED"
    elif "BLOCKED" in verdicts:
        overall = "BLOCKED"
    elif "INCOMPLETE" in verdicts:
        overall = "INCOMPLETE"
    elif verdicts and all(value == "NOT_APPLICABLE" for value in verdicts):
        overall = "NOT_APPLICABLE"
    else:
        overall = "PASSED"
    return {
        "status": "ok",
        "project_id": project_id,
        "components": components,
        "overall_verdict": overall,
        "interpretive_boundary": "Component verdicts retain their own scope. A passing reliability-mechanics run does not imply retrieval acceptance passed.",
    }


def get_run(root: Path, *, run_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    project_id = _project_id(root, name=name, session_id=session_id)
    run = _read(_json_path(root, project_id, run_id))
    return _write_all(root, run)
