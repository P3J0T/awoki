from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import acceptance_runs
import evidence_store
import project_workspace
import safety
import work_ledger

SCHEMA = "awoki-reference-catalog/v1"
MAX_ENTRIES = 512
MAX_LABEL = 240
MAX_WHY = 800
MAX_ALIASES = 16
MAX_LINKS = 32
MAX_SCAN_FILES = 256
RESOLVE_MIN_SCORE = 2.5
RESOLVE_MIN_MARGIN = 1.0
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9._:-]{3,200}$")


def _now() -> str:
    return project_workspace.now_ts()


def _catalog_path(root: Path, project_id: str) -> Path:
    # Human navigation metadata is control-plane state, not RAG content.
    return project_workspace.state_dir(root) / "reference-catalog" / f"{project_id}.json"


def _safe_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()[:limit]
    redacted, _ = safety.redact_text(text)
    return redacted


def _clean_ref(value: str) -> str:
    ref = str(value or "").strip()
    return ref if _REF_RE.fullmatch(ref) else ""


def _load_catalog(root: Path, project_id: str) -> dict[str, Any]:
    path = _catalog_path(root, project_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    entries = value.get("entries") if isinstance(value, dict) else {}
    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "entries": dict(entries) if isinstance(entries, dict) else {},
        "updated_at": str(value.get("updated_at") or "") if isinstance(value, dict) else "",
    }


def _write_catalog(root: Path, project_id: str, state: dict[str, Any]) -> None:
    path = _catalog_path(root, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["schema"] = SCHEMA
    state["project_id"] = project_id
    state["updated_at"] = _now()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _continuity_record(root: Path, project_id: str, reference_id: str) -> dict[str, Any] | None:
    path = project_workspace.paths_for(root, project_id).continuity
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-2048:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("id") or "") == reference_id:
            return row
    return None


def _reliability_object(root: Path, project_id: str, reference_id: str) -> dict[str, Any] | None:
    base = project_workspace.paths_for(root, project_id).project_dir / "reports" / "reliability"
    if not base.is_dir():
        return None
    for path in list(base.glob("*.json"))[-MAX_SCAN_FILES:]:
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(run, dict):
            continue
        for row in run.get("verification_checkpoints") or []:
            if isinstance(row, dict) and str(row.get("checkpoint_id") or "") == reference_id:
                return {"object_type": "verification_checkpoint", "run_id": run.get("run_id"), "row": row}
        for row in run.get("relations") or []:
            if isinstance(row, dict) and str(row.get("relation_id") or "") == reference_id:
                return {"object_type": "relation", "run_id": run.get("run_id"), "row": row}
        for row in run.get("assessments") or []:
            if isinstance(row, dict) and str(row.get("node_id") or "") == reference_id:
                return {"object_type": "assessment", "run_id": run.get("run_id"), "row": row}
    return None


def _candidate_object(root: Path, project_id: str, reference_id: str) -> dict[str, Any] | None:
    """Derive candidate occurrence provenance from immutable captured evidence.

    Candidate IDs are content-addressed across equivalent retrievals. The evidence
    artifacts remain authoritative; this view distinguishes first materialization
    from every observed occurrence instead of pretending one evidence ref is the
    candidate's permanent parent.
    """
    base = project_workspace.paths_for(root, project_id).artifacts_dir / "acceptance" / "raw" / "evidence"
    if not base.is_dir():
        return None
    occurrences: list[dict[str, Any]] = []
    representative: dict[str, Any] | None = None
    scope_identity: dict[str, Any] = {}
    scanned = 0
    for path in sorted(base.glob("ev_*/*.json.gz")):
        # Candidate occurrence provenance is an explicit, on-demand control-plane
        # lookup. Scan the complete project-local evidence set so
        # first_materialized_in covers the complete project-local evidence set rather than a bounded-window guess.
        scanned += 1
        evidence_ref = path.stem.split(".json", 1)[0]
        meta = evidence_store.metadata(root, project_id, evidence_ref)
        if meta.get("status") != "ok":
            continue
        for row in meta.get("candidate_index") or []:
            if isinstance(row, dict) and str(row.get("candidate_id") or "") == reference_id:
                representative = representative or dict(row)
                scope_identity = dict(meta.get("scope_identity") or scope_identity)
                try:
                    artifact_mtime_ns = int(path.stat().st_mtime_ns)
                except OSError:
                    artifact_mtime_ns = 0
                occurrences.append({
                    "evidence_ref": evidence_ref,
                    "observed_at": str(meta.get("created_at") or ""),
                    "artifact_mtime_ns": artifact_mtime_ns,
                    "capture_run_ids": list(meta.get("capture_run_ids") or []),
                })
                break
    if not representative:
        return None
    occurrences.sort(key=lambda row: (
        str(row.get("observed_at") or ""), int(row.get("artifact_mtime_ns") or 0), str(row.get("evidence_ref") or "")
    ))
    return {
        "row": representative,
        "scope_identity": scope_identity,
        "first_materialized_in": str((occurrences[0] if occurrences else {}).get("evidence_ref") or ""),
        "observed_in": [str(row.get("evidence_ref") or "") for row in occurrences[:MAX_LINKS]],
        "occurrences": [
            {key: value for key, value in row.items() if key != "artifact_mtime_ns"}
            for row in occurrences[:MAX_LINKS]
        ],
        "occurrence_total": len(occurrences),
        "occurrence_scan_truncated": False,
    }

def _base_description(root: Path, project_id: str, reference_id: str, *, session_id: str = "") -> dict[str, Any]:
    ref = _clean_ref(reference_id)
    if not ref:
        return {"status": "rejected", "reason": "invalid_reference_id", "reference_id": reference_id}

    if ref.startswith("ev_"):
        meta = evidence_store.metadata(root, project_id, ref)
        if meta.get("status") == "ok":
            tool = str(meta.get("tool") or "Awoki")
            kind = str(meta.get("kind") or "evidence")
            return {
                "status": "ok", "reference_id": ref, "kind": "evidence", "object_kind": kind,
                "label": f"{kind.replace('_', ' ')} from {tool}"[:MAX_LABEL],
                "why_saved": "Content-addressed evidence preserved so exact provenance, candidate, and backend facts can be recovered without rerunning the original operation.",
                "origin": {"tool": tool, "capture_run_ids": list(meta.get("capture_run_ids") or [])},
                "scope": dict(meta.get("scope_identity") or {}),
                "integrity": {"artifact_sha256": meta.get("artifact_sha256"), "payload_sha256": meta.get("payload_sha256")},
                "linked_refs": [str(row.get("candidate_id")) for row in (meta.get("candidate_index") or [])[:8] if isinstance(row, dict) and row.get("candidate_id")],
                "created_at": str(meta.get("created_at") or ""),
            }

    if ref.startswith("acr_"):
        state = acceptance_runs.status(root, run_id=ref, project_id=project_id)
        if state.get("status") == "ok":
            return {
                "status": "ok", "reference_id": ref, "kind": "acceptance_run",
                "label": _safe_text(state.get("title") or state.get("suite") or "Acceptance run", MAX_LABEL),
                "why_saved": "Durable acceptance state preserving exact test contracts, machine outcomes, provenance, evidence references, and compaction continuity.",
                "origin": {"suite": state.get("suite"), "run_status": state.get("run_status")},
                "scope": dict(state.get("scope") or {}),
                "linked_refs": sorted({
                    str(item.get("evidence_ref"))
                    for row in (state.get("records") or {}).values() if isinstance(row, dict)
                    for item in (row.get("evidence_refs") or []) if isinstance(item, dict) and item.get("evidence_ref")
                })[:MAX_LINKS],
                "created_at": str(state.get("created_at") or ""),
            }

    if ref.startswith("aat_"):
        found_attempt = acceptance_runs.attempt_by_id(root, project_id, ref)
        if found_attempt.get("status") == "ok":
            row = dict(found_attempt.get("attempt") or {})
            return {
                "status": "ok", "reference_id": ref, "kind": "acceptance_attempt",
                "label": _safe_text(
                    f"{found_attempt.get('test_id')} attempt {row.get('attempt_number')}: {row.get('outcome')}", MAX_LABEL
                ),
                "why_saved": "Immutable acceptance attempt retained so intermediate machine downgrades and later bookkeeping corrections remain auditable instead of being overwritten by the final test record.",
                "origin": {
                    "acceptance_run_id": found_attempt.get("run_id"),
                    "test_id": found_attempt.get("test_id"),
                    "attempt_number": row.get("attempt_number"),
                    "claimed_outcome": row.get("claimed_outcome"),
                    "effective_outcome": row.get("outcome"),
                    "supersedes_attempt_id": row.get("supersedes_attempt_id") or "",
                },
                "scope": dict(found_attempt.get("scope") or {}),
                "linked_refs": [
                    str(item.get("evidence_ref")) for item in (row.get("evidence_refs") or [])
                    if isinstance(item, dict) and item.get("evidence_ref")
                ][:MAX_LINKS],
                "created_at": str(row.get("recorded_at") or ""),
            }

    if ref.startswith(("vrf_", "rel_", "asn_")):
        found = _reliability_object(root, project_id, ref)
        if found:
            row = dict(found["row"])
            obj = str(found["object_type"])
            if obj == "verification_checkpoint":
                label = f"Verification checkpoint {row.get('iteration')}: {row.get('result')}"
                why = "Deterministic verification checkpoint preserved so provenance/coherence state can be referenced across continuation and compaction."
                links = list(row.get("missing_evidence_refs") or [])
            elif obj == "relation":
                label = f"{row.get('from_node_id')} --{row.get('type')}--> {row.get('to_node_id')}"
                why = "First-class reasoning relation preserved independently from the nodes it connects."
                links = [str(row.get("from_node_id") or ""), str(row.get("to_node_id") or "")]
            else:
                label = str(row.get("statement") or f"{row.get('kind') or 'assessment'} node")
                why = "Reasoning node preserved because it participates in durable investigation or verification state."
                links = [str(item.get("evidence_ref")) for item in (row.get("evidence_refs") or []) if isinstance(item, dict) and item.get("evidence_ref")]
            return {
                "status": "ok", "reference_id": ref, "kind": obj,
                "label": _safe_text(label, MAX_LABEL), "why_saved": _safe_text(why, MAX_WHY),
                "origin": {"reliability_run_id": found.get("run_id")}, "scope": {"project_id": project_id},
                "linked_refs": [item for item in links if item][:MAX_LINKS],
                "created_at": str(row.get("recorded_at") or ""),
            }

    if ref.startswith("cand_"):
        found = _candidate_object(root, project_id, ref)
        if found:
            row = dict(found["row"])
            target = row.get("qualified_name") or row.get("symbol_name") or row.get("symbol") or row.get("path") or "retrieval candidate"
            observed_in = list(found.get("observed_in") or [])
            first_materialized = str(found.get("first_materialized_in") or "")
            return {
                "status": "ok", "reference_id": ref, "kind": "retrieval_candidate",
                "label": _safe_text(target, MAX_LABEL),
                "why_saved": "Typed candidate identity preserved across captured retrieval evidence; occurrence provenance distinguishes first materialization from later observations without changing cand_ identity.",
                "origin": {
                    "first_materialized_in": first_materialized,
                    "observed_in": observed_in,
                    "occurrence_count": int(found.get("occurrence_total") or len(found.get("occurrences") or [])),
                    "occurrence_scan_truncated": bool(found.get("occurrence_scan_truncated")),
                },
                "scope": dict(found.get("scope_identity") or {}),
                "linked_refs": observed_in[:MAX_LINKS],
            }

    if ref.startswith("atd_") and session_id:
        work = work_ledger.status(root, session_id)
        for row in work.get("todos") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == ref:
                return {
                    "status": "ok", "reference_id": ref, "kind": "session_todo",
                    "label": _safe_text(row.get("content") or "Session TODO", MAX_LABEL),
                    "why_saved": "Session-local operational work item preserved across compaction without promoting it to canonical project knowledge.",
                    "origin": {"session_local": True, "status": row.get("status")},
                    "scope": {"project_id": project_id}, "linked_refs": [],
                }

    if ref.startswith("cont_"):
        row = _continuity_record(root, project_id, ref)
        if row:
            return {
                "status": "ok", "reference_id": ref, "kind": "project_continuity",
                "label": _safe_text(row.get("summary") or row.get("text") or "Project continuity record", MAX_LABEL),
                "why_saved": "Project continuity record preserved for later recall, correction, supersession, and provenance-aware continuation.",
                "origin": {"record_kind": row.get("kind"), "state": row.get("state")},
                "scope": {"project_id": project_id},
                "linked_refs": [str(item) for item in (row.get("supersedes") or [])[:MAX_LINKS] if str(item)],
                "created_at": str(row.get("created_at") or row.get("timestamp") or ""),
            }

    return {"status": "not_found", "reference_id": ref, "project_id": project_id}


def annotate(
    root: Path,
    project_id: str,
    reference_id: str,
    *,
    label: str = "",
    why_saved: str = "",
    aliases: list[str] | None = None,
    linked_refs: list[str] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    base = _base_description(root, project_id, reference_id, session_id=session_id)
    if base.get("status") != "ok":
        return base
    clean_label = _safe_text(label or base.get("label") or "", MAX_LABEL)
    clean_why = _safe_text(why_saved or base.get("why_saved") or "", MAX_WHY)
    clean_aliases = list(dict.fromkeys(_safe_text(item, MAX_LABEL) for item in (aliases or [])[:MAX_ALIASES] if str(item).strip()))
    clean_links = list(dict.fromkeys(ref for ref in (_clean_ref(item) for item in (linked_refs or [])[:MAX_LINKS]) if ref))
    state = _load_catalog(root, project_id)
    entries = dict(state.get("entries") or {})
    entries[str(base["reference_id"])] = {
        "reference_id": str(base["reference_id"]), "label": clean_label, "why_saved": clean_why,
        "aliases": clean_aliases, "linked_refs": clean_links, "updated_at": _now(),
    }
    if len(entries) > MAX_ENTRIES:
        ordered = sorted(entries.values(), key=lambda row: str((row or {}).get("updated_at") or ""))[-MAX_ENTRIES:]
        entries = {str(row["reference_id"]): row for row in ordered if isinstance(row, dict) and row.get("reference_id")}
    state["entries"] = entries
    _write_catalog(root, project_id, state)
    return describe(root, project_id, str(base["reference_id"]), session_id=session_id)


def describe(root: Path, project_id: str, reference_id: str, *, session_id: str = "") -> dict[str, Any]:
    base = _base_description(root, project_id, reference_id, session_id=session_id)
    if base.get("status") != "ok":
        return base
    entry = dict((_load_catalog(root, project_id).get("entries") or {}).get(str(base["reference_id"])) or {})
    if entry:
        if entry.get("label"):
            base["label"] = entry["label"]
        if entry.get("why_saved"):
            base["why_saved"] = entry["why_saved"]
        base["aliases"] = list(entry.get("aliases") or [])
        base["linked_refs"] = list(dict.fromkeys(list(base.get("linked_refs") or []) + list(entry.get("linked_refs") or [])))[:MAX_LINKS]
        base["annotation_updated_at"] = str(entry.get("updated_at") or "")
    else:
        base["aliases"] = []
    base["reference_contract"] = "Stable ID is authoritative; label/why_saved/aliases are human navigation metadata and never replace provenance or evidence identity."
    return base


def _candidate_descriptors(root: Path, project_id: str, *, session_id: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    catalog = _load_catalog(root, project_id)
    for ref in list((catalog.get("entries") or {}).keys())[-MAX_ENTRIES:]:
        desc = describe(root, project_id, ref, session_id=session_id)
        if desc.get("status") == "ok":
            rows.append(desc)

    # Acceptance runs are cheap to enumerate and provide strong human anchors.
    # Only evidence already linked by those compact ledgers is considered for
    # unannotated resolution; we never scan/decompress the entire evidence store.
    acceptance_dir = project_workspace.paths_for(root, project_id).artifacts_dir / "acceptance"
    linked_evidence: list[str] = []
    for path in list(acceptance_dir.glob("acr_*.json"))[-128:] if acceptance_dir.is_dir() else []:
        desc = describe(root, project_id, path.stem, session_id=session_id)
        if desc.get("status") == "ok":
            rows.append(desc)
            for ref in desc.get("linked_refs") or []:
                if str(ref).startswith("ev_") and ref not in linked_evidence:
                    linked_evidence.append(str(ref))
    for ref in linked_evidence[-64:]:
        desc = describe(root, project_id, ref, session_id=session_id)
        if desc.get("status") == "ok":
            rows.append(desc)

    # Reliability descriptors are built from already-open compact JSON rather than
    # recursively rescanning the reliability directory for every node/relation.
    base = project_workspace.paths_for(root, project_id).project_dir / "reports" / "reliability"
    if base.is_dir():
        for path in list(base.glob("*.json"))[-64:]:
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run_id = str(run.get("run_id") or path.stem)
            for row in list(run.get("verification_checkpoints") or [])[-16:]:
                if not isinstance(row, dict) or not row.get("checkpoint_id"):
                    continue
                rows.append({
                    "status": "ok", "reference_id": str(row["checkpoint_id"]), "kind": "verification_checkpoint",
                    "label": _safe_text(f"Verification checkpoint {row.get('iteration')}: {row.get('result')}", MAX_LABEL),
                    "why_saved": "Deterministic verification checkpoint preserved so provenance/coherence state can be referenced across continuation and compaction.",
                    "aliases": [], "origin": {"reliability_run_id": run_id}, "scope": {"project_id": project_id},
                    "linked_refs": list(row.get("missing_evidence_refs") or [])[:MAX_LINKS],
                })
            for row in list(run.get("relations") or [])[-32:]:
                if not isinstance(row, dict) or not row.get("relation_id"):
                    continue
                rows.append({
                    "status": "ok", "reference_id": str(row["relation_id"]), "kind": "relation",
                    "label": _safe_text(f"{row.get('from_node_id')} --{row.get('type')}--> {row.get('to_node_id')}", MAX_LABEL),
                    "why_saved": "First-class reasoning relation preserved independently from the nodes it connects.",
                    "aliases": [], "origin": {"reliability_run_id": run_id}, "scope": {"project_id": project_id},
                    "linked_refs": [str(row.get("from_node_id") or ""), str(row.get("to_node_id") or "")],
                })
            for row in list(run.get("assessments") or [])[-32:]:
                if not isinstance(row, dict) or not row.get("node_id"):
                    continue
                rows.append({
                    "status": "ok", "reference_id": str(row["node_id"]), "kind": "assessment",
                    "label": _safe_text(row.get("statement") or f"{row.get('kind') or 'assessment'} node", MAX_LABEL),
                    "why_saved": "Reasoning node preserved because it participates in durable investigation or verification state.",
                    "aliases": [], "origin": {"reliability_run_id": run_id}, "scope": {"project_id": project_id},
                    "linked_refs": [str(item.get("evidence_ref")) for item in (row.get("evidence_refs") or []) if isinstance(item, dict) and item.get("evidence_ref")][:MAX_LINKS],
                })
    continuity_path = project_workspace.paths_for(root, project_id).continuity
    try:
        continuity_lines = continuity_path.read_text(encoding="utf-8").splitlines()[-256:]
    except OSError:
        continuity_lines = []
    for line in continuity_lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not str(row.get("id") or "").startswith("cont_"):
            continue
        rows.append({
            "status": "ok", "reference_id": str(row["id"]), "kind": "project_continuity",
            "label": _safe_text(row.get("summary") or row.get("text") or "Project continuity record", MAX_LABEL),
            "why_saved": "Project continuity record preserved for later recall, correction, supersession, and provenance-aware continuation.",
            "aliases": [], "origin": {"record_kind": row.get("kind"), "state": row.get("state")},
            "scope": {"project_id": project_id}, "linked_refs": [str(item) for item in (row.get("supersedes") or [])[:MAX_LINKS] if str(item)],
        })

    if session_id:
        for row in work_ledger.status(root, session_id).get("todos") or []:
            if isinstance(row, dict) and row.get("id"):
                desc = describe(root, project_id, str(row["id"]), session_id=session_id)
                if desc.get("status") == "ok":
                    rows.append(desc)

    dedup: dict[str, dict[str, Any]] = {}
    # Catalog annotations win because they carry deliberate human labels/aliases.
    annotated_ids = set((catalog.get("entries") or {}).keys())
    for row in rows:
        ref = str(row.get("reference_id") or "")
        if not ref:
            continue
        if ref not in dedup or ref in annotated_ids:
            if ref in annotated_ids:
                desc = describe(root, project_id, ref, session_id=session_id)
                dedup[ref] = desc if desc.get("status") == "ok" else row
            else:
                dedup[ref] = row
    return list(dedup.values())


def resolve(root: Path, project_id: str, query: str, *, limit: int = 8, session_id: str = "") -> dict[str, Any]:
    q = _safe_text(query, 600).strip()
    if not q:
        return {"status": "rejected", "reason": "query_required"}
    exact = _clean_ref(q)
    if exact:
        described = describe(root, project_id, exact, session_id=session_id)
        matches = [described] if described.get("status") == "ok" else []
        return {
            "status": "ok", "query": q, "matches": matches,
            "resolution": {
                "status": "exact" if matches else "not_found",
                "resolved_reference_id": exact if matches else "",
                "reason": "stable_reference_id_supplied" if matches else "stable_reference_not_found",
            },
            "resolved_reference_id": exact if matches else "",
            "resolution_boundary": "Natural-language resolution is navigation only. Use the returned stable reference_id for authoritative retrieval/verification.",
        }
    terms = [term for term in re.findall(r"[a-z0-9_.:-]+", q.lower()) if len(term) >= 2]
    matches: list[tuple[float, dict[str, Any]]] = []
    for row in _candidate_descriptors(root, project_id, session_id=session_id):
        hay = " ".join([
            str(row.get("reference_id") or ""), str(row.get("label") or ""), str(row.get("why_saved") or ""),
            " ".join(str(item) for item in row.get("aliases") or []), json.dumps(row.get("origin") or {}, ensure_ascii=False),
        ]).lower()
        score = 0.0
        if q.lower() in hay:
            score += 10.0
        for term in terms:
            if term in hay:
                score += 1.0 + min(len(term), 20) / 20.0
        if score > 0:
            matches.append((score, row))
    matches.sort(key=lambda item: (-item[0], str(item[1].get("reference_id") or "")))
    picked = []
    for score, row in matches[: max(1, min(int(limit or 8), 20))]:
        clean = dict(row)
        clean["match_score"] = round(score, 3)
        picked.append(clean)

    top = float(matches[0][0]) if matches else 0.0
    second = float(matches[1][0]) if len(matches) > 1 else 0.0
    margin = top - second if matches else 0.0
    if not matches:
        resolution_status = "not_found"
        reason = "no_matching_reference"
        resolved = ""
    elif top < RESOLVE_MIN_SCORE:
        resolution_status = "ambiguous"
        reason = "top_match_below_confidence_threshold"
        resolved = ""
    elif len(matches) > 1 and margin < RESOLVE_MIN_MARGIN:
        resolution_status = "ambiguous"
        reason = "top_matches_too_close"
        resolved = ""
    else:
        resolution_status = "resolved"
        reason = "single_clear_navigation_match"
        resolved = str(matches[0][1].get("reference_id") or "")
    return {
        "status": "ok", "query": q, "project_id": project_id, "matches": picked,
        "resolution": {
            "status": resolution_status,
            "resolved_reference_id": resolved,
            "top_score": round(top, 3), "second_score": round(second, 3), "margin": round(margin, 3),
            "minimum_score": RESOLVE_MIN_SCORE, "minimum_margin": RESOLVE_MIN_MARGIN,
            "reason": reason,
        },
        "resolved_reference_id": resolved,
        "resolution_boundary": "Natural-language resolution is navigation only. Ambiguous phrases do not resolve to an authoritative object; use a returned stable reference_id for authoritative retrieval/verification.",
    }

def compact_context(root: Path, project_id: str, *, session_id: str = "", max_chars: int = 3200) -> str:
    """Return a tiny current-session human navigation map for compaction.

    This contains labels/why_saved/stable IDs only. It never opens rich evidence
    payloads and never changes the authority of the referenced object.  Deliberate
    annotations from older sessions remain searchable in the project catalog but
    are not injected merely because they were recently saved.
    """
    if not project_id:
        return ""
    rows: list[dict[str, str]] = []
    active = acceptance_runs.status(root, session_id=session_id) if session_id else {}
    if active.get("status") == "ok" and str(active.get("project_id") or "") == project_id:
        rows.append({
            "reference_id": str(active.get("run_id") or ""),
            "label": _safe_text(active.get("title") or active.get("suite") or "Acceptance run", MAX_LABEL),
            "why_saved": "Durable acceptance state and exact machine-enforced test contract.",
        })
        evidence_rows: dict[str, dict[str, Any]] = {}
        for record in (active.get("records") or {}).values():
            if not isinstance(record, dict):
                continue
            for ref in record.get("evidence_refs") or []:
                if isinstance(ref, dict) and ref.get("evidence_ref"):
                    evidence_rows[str(ref["evidence_ref"])] = ref
        for ref_id, meta in list(evidence_rows.items())[-6:]:
            rows.append({
                "reference_id": ref_id,
                "label": _safe_text(f"{meta.get('kind') or 'evidence'} from {meta.get('tool') or 'Awoki'}", MAX_LABEL),
                "why_saved": "Referenced by the active acceptance run; retrieve by stable ID instead of reconstructing it from chat memory.",
            })

    references_need_review = False
    if session_id:
        work = work_ledger.status(root, session_id)
        references_need_review = bool(work.get("references_need_review"))
        session_rows = [
            dict(row) for row in (work.get("active_references") or [])
            if isinstance(row, dict) and str(row.get("project_id") or "") == project_id
        ]
        for row in session_rows[-8:]:
            ref_id = str(row.get("reference_id") or "")
            if not ref_id or any(existing["reference_id"] == ref_id for existing in rows):
                continue
            rows.append({
                "reference_id": ref_id,
                "label": _safe_text(row.get("label") or ref_id, MAX_LABEL),
                "why_saved": _safe_text(row.get("why_saved") or "Current-session navigation reference.", 320),
            })
    if not rows:
        return ""
    lines = [
        "## Awoki current-session references",
        "",
        "Stable IDs are authoritative. This working set contains only references used in the current session plus any active acceptance run; older project references remain searchable but are not injected by recency alone.",
        f"reference_set_needs_review: {'true' if references_need_review else 'false'}",
    ]
    if references_need_review:
        lines.append("These references predate the newest user turn. Reconcile them with the newest direction before treating them as active context.")
    for row in rows[:10]:
        lines.append(f"- {row['reference_id']} — {row['label']} — why: {row['why_saved']}")
    return "\n".join(lines)[: max(800, min(int(max_chars or 3200), 6000))]
