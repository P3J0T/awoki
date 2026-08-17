from __future__ import annotations

import asyncio
import sys
from typing import Any

try:
    from mcp_runtime import installed_mcp_version

    _mcp_sdk_version = installed_mcp_version()
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover - exercised only when dependency missing/incompatible
    print(f"Failed to initialize Awoki MCP runtime: {exc}", file=sys.stderr)
    print("Rebuild the image from requirements.txt and run .harness/bin/mcp-preflight.", file=sys.stderr)
    raise

import rag_backend
import burp
import reliability
import claim_graph
from code_search.vector_store import code_collection_name

from harness_core import (
    HarnessPaths,
    approve_promotion as core_approve_promotion,
    index_all as core_index_all,
    index_global as core_index_global,
    index_project as core_index_project,
    classify_memory_text,
    demote_global_memory as core_demote_global_memory,
    harness_status as core_harness_status,
    harness_self_check as core_harness_self_check,
    list_promotion_candidates as core_list_promotion_candidates,
    load_manifest as core_load_manifest,
    load_skill as core_load_skill,
    propose_skill_update as core_propose_skill_update,
    project_capture as core_project_capture,
    project_continuation_schedule as core_project_continuation_schedule,
    project_continuation_status as core_project_continuation_status,
    project_continuation_cancel as core_project_continuation_cancel,
    project_continuation_finalize as core_project_continuation_finalize,
    project_task_checkpoint as core_project_task_checkpoint,
    project_task_status as core_project_task_status,
    project_task_finalize as core_project_task_finalize,
    session_work_status as core_session_work_status,
    session_runtime_status as core_session_runtime_status,
    reference_describe as core_reference_describe,
    reference_annotate as core_reference_annotate,
    reference_resolve as core_reference_resolve,
    acceptance_run_start as core_acceptance_run_start,
    acceptance_run_status as core_acceptance_run_status,
    acceptance_run_next as core_acceptance_run_next,
    acceptance_evidence_get as core_acceptance_evidence_get,
    acceptance_run_record as core_acceptance_run_record,
    acceptance_run_record_invariant as core_acceptance_run_record_invariant,
    acceptance_run_finalize as core_acceptance_run_finalize,
    codebase_search as core_codebase_search,
    code_diagnostics_trace as core_code_diagnostics_trace,
    code_index_status as core_code_index_status,
    code_index_verify as core_code_index_verify,
    code_definition as core_code_definition,
    code_callers as core_code_callers,
    code_callees as core_code_callees,
    code_path as core_code_path,
    code_flow_graph as core_code_flow_graph,
    code_source_window as core_code_source_window,
    code_evidence_verify as core_code_evidence_verify,
    code_semantics_check as core_code_semantics_check,
    code_exact_search as core_code_exact_search,
    code_text_search as core_code_text_search,
    cross_project_code_search as core_cross_project_code_search,
    code_validate_claim as core_code_validate_claim,
    code_evaluate as core_code_evaluate,
    project_create as core_project_create,
    project_open as core_project_open,
    project_repo_add as core_project_repo_add,
    project_repo_list as core_project_repo_list,
    project_repo_remove as core_project_repo_remove,
    project_repo_default as core_project_repo_default,
    project_source_add as core_project_source_add,
    project_source_list as core_project_source_list,
    project_source_remove as core_project_source_remove,
    project_source_default as core_project_source_default,
    project_pause as core_project_pause,
    project_refresh as core_project_refresh,
    code_index_refresh_start as core_code_index_refresh_start,
    code_index_refresh_status as core_code_index_refresh_status,
    code_index_refresh_cancel as core_code_index_refresh_cancel,
    code_vector_refresh_start as core_code_vector_refresh_start,
    code_vector_refresh_status as core_code_vector_refresh_status,
    code_vector_refresh_cancel as core_code_vector_refresh_cancel,
    repository_prepare_start as core_repository_prepare_start,
    repository_prepare_status as core_repository_prepare_status,
    repository_prepare_cancel as core_repository_prepare_cancel,
    project_search as core_project_search,
    project_index_preview as core_project_index_preview,
    project_handoff as core_project_handoff,
    project_index as core_project_index,
    project_list as core_project_list,
    project_migrate as core_project_migrate,
    project_mark_pending as core_project_mark_pending,
    project_note as core_project_note,
    project_pending as core_project_pending,
    project_resume as core_project_resume,
    project_status as core_project_status,
    open_artifact as core_open_artifact,
    propose_promotion as core_propose_promotion,
    recall_context as core_recall_context,
    reject_promotion as core_reject_promotion,
    save_finding as core_save_finding,
    save_global_fact as core_save_global_fact,
    save_hypothesis as core_save_hypothesis,
    save_project_fact as core_save_project_fact,
    search_evidence as core_search_evidence,
    search_rag as core_search_rag,
    search_records,
    project_records,
    global_records,
    search_skills as core_search_skills,
)

mcp = FastMCP("awoki")


@mcp.tool()
def harness_status(session_id: str = "") -> dict[str, Any]:
    """Return session-scoped attachment, harness paths, memory scopes, skill counts, and recommended next calls."""
    return core_harness_status(session_id=session_id)


@mcp.tool()
def harness_self_check(check: str, session_id: str = "") -> dict[str, Any]:
    """Run one allow-listed hermetic Awoki regression check; this is never a generic shell runner."""
    return core_harness_self_check(check=check)


@mcp.tool()
def load_manifest() -> dict[str, Any]:
    """Return the machine-readable harness manifest."""
    return core_load_manifest()






@mcp.tool()
def project_open(name: str, create_if_missing: bool = False, session_id: str = "") -> dict[str, Any]:
    """Open/resume/create a project with a slim orientation projection: repo/readiness, active work, and prior-material pointers. Use project_resume for dense continuity."""
    return core_project_open(name=name, create_if_missing=create_if_missing, session_id=session_id)


@mcp.tool()
def project_repo_add(
    repo_id: str,
    path: str = "",
    name: str = "",
    make_default: bool = False,
    session_id: str = "",
) -> dict[str, Any]:
    """Register a repository under the active project. Empty path infers repo/<repo_id>; existing Git roots are verified exactly."""
    return core_project_repo_add(
        repo_id=repo_id,
        path=path,
        name=name,
        make_default=make_default,
        session_id=session_id,
    )


@mcp.tool()
def project_repo_list(name: str = "", session_id: str = "") -> dict[str, Any]:
    """List the active project's registered repositories, default repository, and passive code/vector readiness."""
    return core_project_repo_list(name=name, session_id=session_id)


@mcp.tool()
def project_repo_remove(repo_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Remove one repository registration without deleting its files."""
    return core_project_repo_remove(repo_id=repo_id, name=name, session_id=session_id)


@mcp.tool()
def project_repo_default(repo_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Set the default registered repository for the active project."""
    return core_project_repo_default(repo_id=repo_id, name=name, session_id=session_id)


@mcp.tool()
def project_source_add(
    source_id: str,
    path: str = "",
    source_type: str = "directory",
    name: str = "",
    make_default: bool = False,
    session_id: str = "",
) -> dict[str, Any]:
    """Register a non-Git evidence corpus under project sources/. Empty path infers sources/<source_id>."""
    return core_project_source_add(
        source_id=source_id,
        path=path,
        source_type=source_type,
        name=name,
        make_default=make_default,
        session_id=session_id,
    )


@mcp.tool()
def project_source_list(name: str = "", session_id: str = "") -> dict[str, Any]:
    """List all registered evidence sources. Git repositories appear as source_type=git."""
    return core_project_source_list(name=name, session_id=session_id)


@mcp.tool()
def project_source_remove(source_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Remove one non-Git source registration without deleting its files."""
    return core_project_source_remove(source_id=source_id, name=name, session_id=session_id)


@mcp.tool()
def project_source_default(source_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Set the default registered evidence source for the active project."""
    return core_project_source_default(source_id=source_id, name=name, session_id=session_id)


@mcp.tool()
def project_capture(
    summary: str,
    name: str = "",
    details: str = "",
    kind: str = "observation",
    sources: list[Any] | None = None,
    confidence: str = "medium",
    sensitivity: str = "project",
    index_policy: str = "safe",
    tags: list[str] | None = None,
    uncertainty: list[str] | None = None,
    likely_continuation: str = "",
    supersedes: list[str] | None = None,
    state: str = "",
    metadata: dict[str, Any] | None = None,
    allow_sensitive_plaintext: bool = False,
    session_id: str = "",
) -> dict[str, Any]:
    """Capture one concise continuity record. Generic saves should use the neutral default kind=observation; evidence-oriented finding/discovery labels are explicit. This never stores private chain-of-thought or silently falls back to legacy memory."""
    return core_project_capture(
        summary=summary,
        name=name,
        details=details,
        kind=kind,
        sources=sources,
        confidence=confidence,
        sensitivity=sensitivity,
        index_policy=index_policy,
        tags=tags,
        uncertainty=uncertainty,
        likely_continuation=likely_continuation,
        supersedes=supersedes,
        state=state,
        metadata=metadata,
        allow_sensitive_plaintext=allow_sensitive_plaintext,
        session_id=session_id,
    )


@mcp.tool()
def project_search(query: str, name: str = "", include_global: bool = False, limit: int = 10, session_id: str = "") -> dict[str, Any]:
    """Search safe project continuity first, with clearly labeled optional global reusable knowledge."""
    return core_project_search(query=query, name=name, include_global=include_global, limit=limit, session_id=session_id)


@mcp.tool()
async def project_refresh(name: str = "", reason: str = "", include_artifacts: bool = True, include_code: bool = False, include_qdrant: bool = False, session_id: str = "") -> dict[str, Any]:
    """Regenerate SITUATION/HANDOFF and rebuild requested indexes without blocking the MCP event loop."""
    return await asyncio.to_thread(
        core_project_refresh,
        name=name, reason=reason, include_artifacts=include_artifacts, include_code=include_code,
        include_qdrant=include_qdrant, session_id=session_id,
    )


@mcp.tool()
def code_index_refresh_start(name: str = "", repo: str = "", source_id: str = "", force: bool = False, session_id: str = "") -> dict[str, Any]:
    """Start detached local structural/FTS indexing for one Git repo, all active Git repos, or one explicit non-Git evidence source. Returns immediately with a job id and performs no remote embedding/Qdrant work."""
    return core_code_index_refresh_start(name=name, repo=repo, source_id=source_id, force=force, session_id=session_id)


@mcp.tool()
def code_index_refresh_status(name: str = "", repo: str = "", source_id: str = "", job_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Check a detached local structural/FTS index refresh. Returns bounded file/parser progress without source text. Do not call repeatedly in an autonomous polling loop."""
    return core_code_index_refresh_status(name=name, repo=repo, source_id=source_id, job_id=job_id, session_id=session_id)


@mcp.tool()
def code_index_refresh_cancel(job_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Cancel a detached local structural/FTS index refresh job."""
    return core_code_index_refresh_cancel(job_id=job_id, name=name, session_id=session_id)


@mcp.tool()
def code_vector_refresh_start(name: str = "", repo: str = "", source_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Start detached semantic code-vector materialization for one Git repo, all active Git repos, or one explicit evidence source; returns immediately with a job id. Non-Git sources require source_id so source registration never implies remote upload permission."""
    return core_code_vector_refresh_start(name=name, repo=repo, source_id=source_id, session_id=session_id)


@mcp.tool()
def code_vector_refresh_status(name: str = "", repo: str = "", source_id: str = "", job_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Check a detached semantic code-vector refresh job on demand. Returns phase plus chunk/vector/batch progress without source text. Do not call repeatedly in an autonomous polling loop."""
    return core_code_vector_refresh_status(name=name, repo=repo, source_id=source_id, job_id=job_id, session_id=session_id)


@mcp.tool()
def code_vector_refresh_cancel(job_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Cancel a detached semantic code-vector refresh job."""
    return core_code_vector_refresh_cancel(job_id=job_id, name=name, session_id=session_id)


@mcp.tool()
def repository_prepare_start(
    name: str = "", repo: str = "", source_id: str = "", mode: str = "full",
    resume_goal: str = "", session_id: str = "",
) -> dict[str, Any]:
    """Start detached end-to-end repository readiness; full mode explicitly authorizes semantic materialization for this exact managed scope."""
    return core_repository_prepare_start(
        name=name, repo=repo, source_id=source_id, mode=mode, resume_goal=resume_goal, session_id=session_id
    )


@mcp.tool()
def repository_prepare_status(
    name: str = "", repo: str = "", source_id: str = "", mode: str = "full",
    job_id: str = "", session_id: str = "",
) -> dict[str, Any]:
    """Return bounded parent/child progress for end-to-end repository readiness."""
    return core_repository_prepare_status(
        name=name, repo=repo, source_id=source_id, mode=mode, job_id=job_id, session_id=session_id
    )


@mcp.tool()
def repository_prepare_cancel(job_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Explicitly cancel a repository preparation parent job and its active child."""
    return core_repository_prepare_cancel(job_id=job_id, name=name, session_id=session_id)


@mcp.tool()
def project_continuation_schedule(
    workflow: str, phase: str, wait_tool: str, wait_job_id: str, wait_seconds: int,
    name: str = "", repo: str = "", source_id: str = "", next_action: str = "",
    resume_goal: str = "", auto_resume: bool = True, session_id: str = "",
) -> dict[str, Any]:
    """Schedule optional session continuation for one detached Awoki job. Polling is local; conversation resume is best-effort and not a job-correctness boundary."""
    return core_project_continuation_schedule(
        workflow=workflow, phase=phase, wait_tool=wait_tool, wait_job_id=wait_job_id,
        wait_seconds=wait_seconds, name=name, repo=repo, source_id=source_id,
        next_action=next_action, resume_goal=resume_goal, auto_resume=auto_resume,
        session_id=session_id,
    )


@mcp.tool()
def project_continuation_status(session_id: str = "") -> dict[str, Any]:
    """Return optional session-continuation state for this OpenCode session."""
    return core_project_continuation_status(session_id=session_id)


@mcp.tool()
def project_continuation_cancel(reason: str = "cancelled", session_id: str = "") -> dict[str, Any]:
    """Cancel this session's auto-continuation. This does not cancel the underlying detached job."""
    return core_project_continuation_cancel(reason=reason, session_id=session_id)


@mcp.tool()
def project_continuation_finalize(reason: str = "completed", session_id: str = "") -> dict[str, Any]:
    """Finalize this session's auto-continuation after readiness or the resumed goal reaches a stable handoff."""
    return core_project_continuation_finalize(reason=reason, session_id=session_id)

@mcp.tool()
def session_work_status(session_id: str = "") -> dict[str, Any]:
    """Return the durable OpenCode TODO/work snapshot for this session, including unattached/ad-hoc work."""
    return core_session_work_status(session_id=session_id)


@mcp.tool()
def reference_describe(reference_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Describe a stable Awoki ID with a compact human label, why it exists, provenance, and linked refs."""
    return core_reference_describe(reference_id=reference_id, name=name, session_id=session_id)


@mcp.tool()
def reference_annotate(
    reference_id: str, label: str = "", why_saved: str = "", aliases: list[str] | None = None,
    linked_refs: list[str] | None = None, name: str = "", session_id: str = "",
) -> dict[str, Any]:
    """Attach human navigation metadata to an existing stable Awoki ID without changing its identity."""
    return core_reference_annotate(
        reference_id=reference_id, label=label, why_saved=why_saved, aliases=aliases, linked_refs=linked_refs,
        name=name, session_id=session_id,
    )


@mcp.tool()
def reference_resolve(query: str, name: str = "", limit: int = 8, session_id: str = "") -> dict[str, Any]:
    """Resolve a natural-language reference phrase to bounded candidate stable IDs; stable IDs remain authoritative."""
    return core_reference_resolve(query=query, name=name, limit=limit, session_id=session_id)


@mcp.tool()
def acceptance_run_start(
    suite: str,
    title: str = "",
    expected_tests: list[str] | None = None,
    expected_invariants: list[str] | None = None,
    test_plan: list[dict[str, Any]] | None = None,
    name: str = "",
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Start a durable project-scoped acceptance ledger bound to the current managed source identity."""
    return core_acceptance_run_start(
        suite=suite, title=title, expected_tests=expected_tests, expected_invariants=expected_invariants, test_plan=test_plan, name=name, repo=repo, source_id=source_id, session_id=session_id
    )



@mcp.tool()
def session_runtime_status(session_id: str = "") -> dict[str, Any]:
    """Return structural agent-turn anomaly/recovery state without exposing reasoning content."""
    return core_session_runtime_status(session_id=session_id)


@mcp.tool()
def acceptance_run_next(run_id: str = "", name: str = "", session_id: str = "") -> dict[str, Any]:
    """Return the next unfinished acceptance step so models do not need to re-plan the suite scheduler."""
    return core_acceptance_run_next(run_id=run_id, name=name, session_id=session_id)

@mcp.tool()
def acceptance_run_status(run_id: str = "", name: str = "", session_id: str = "") -> dict[str, Any]:
    """Return active or named acceptance evidence so reporting never depends on compacted chat memory."""
    return core_acceptance_run_status(run_id=run_id, name=name, session_id=session_id)


@mcp.tool()
def acceptance_evidence_get(
    evidence_ref: str, run_id: str = "", name: str = "", selector: str = "payload",
    offset: int = 0, limit: int = 20, max_chars: int = 20000, session_id: str = "",
) -> dict[str, Any]:
    """Retrieve an exact Awoki-produced evidence artifact by stable ref, with bounded selector/pagination."""
    return core_acceptance_evidence_get(
        evidence_ref=evidence_ref, run_id=run_id, name=name, selector=selector,
        offset=offset, limit=limit, max_chars=max_chars, session_id=session_id,
    )


@mcp.tool()
def acceptance_run_record(
    run_id: str,
    test_id: str,
    outcome: str,
    name: str = "",
    query: str = "",
    targets: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    candidate_ids: list[str] | None = None,
    primary_candidate_id: str = "",
    notes: str = "",
    violations: list[str] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """Persist one compact acceptance observation, optionally bound to captured evidence/canonical candidate IDs."""
    return core_acceptance_run_record(
        run_id=run_id, test_id=test_id, outcome=outcome, name=name, query=query, targets=targets,
        evidence=evidence, evidence_refs=evidence_refs, candidate_ids=candidate_ids,
        primary_candidate_id=primary_candidate_id, notes=notes, violations=violations, session_id=session_id,
    )


@mcp.tool()
def acceptance_run_record_invariant(
    run_id: str, invariant_id: str, outcome: str, name: str = "",
    evidence: dict[str, Any] | None = None, evidence_refs: list[str] | None = None, session_id: str = "",
) -> dict[str, Any]:
    """Persist one compact cross-test invariant, optionally referencing captured Awoki evidence."""
    return core_acceptance_run_record_invariant(
        run_id=run_id, invariant_id=invariant_id, outcome=outcome, name=name, evidence=evidence,
        evidence_refs=evidence_refs, session_id=session_id
    )


@mcp.tool()
def acceptance_run_finalize(run_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Finalize only a complete acceptance ledger; incomplete runs are rejected and remain resumable."""
    return core_acceptance_run_finalize(run_id=run_id, name=name, session_id=session_id)


@mcp.tool()
def project_task_checkpoint(
    title: str,
    name: str = "",
    status: str = "running",
    current_step: str = "",
    completed_steps: list[str] | None = None,
    remaining_steps: list[str] | None = None,
    next_action: str = "",
    last_tool_output_summary: str = "",
    related_refs: list[str] | None = None,
    task_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Checkpoint generic long-running project work. Use this for code/docs/research tasks; Burp task tools are Burp-only compatibility helpers."""
    return core_project_task_checkpoint(
        title=title, name=name, status=status, current_step=current_step,
        completed_steps=completed_steps, remaining_steps=remaining_steps,
        next_action=next_action, last_tool_output_summary=last_tool_output_summary,
        related_refs=related_refs, task_id=task_id, session_id=session_id,
    )


@mcp.tool()
def project_task_status(task_id: str = "", name: str = "", session_id: str = "") -> dict[str, Any]:
    """Return the latest generic project-task checkpoint for deterministic continuation."""
    return core_project_task_status(task_id=task_id, name=name, session_id=session_id)


@mcp.tool()
def project_task_finalize(
    task_id: str = "", name: str = "", outcome: str = "", finding: str = "",
    next_action: str = "", session_id: str = "",
) -> dict[str, Any]:
    """Finalize a generic project task and persist a concise continuity result."""
    return core_project_task_finalize(
        task_id=task_id, name=name, outcome=outcome, finding=finding,
        next_action=next_action, session_id=session_id,
    )


@mcp.tool()
def project_pause(
    name: str = "",
    summary: str = "",
    details: str = "",
    sources: list[Any] | None = None,
    uncertainty: list[str] | None = None,
    likely_continuation: str = "",
    confidence: str = "medium",
    session_id: str = "",
) -> dict[str, Any]:
    """Optionally capture an operational reflection, regenerate continuity views, and detach the current session."""
    return core_project_pause(
        name=name,
        summary=summary,
        details=details,
        sources=sources,
        uncertainty=uncertainty,
        likely_continuation=likely_continuation,
        confidence=confidence,
        session_id=session_id,
    )


@mcp.tool()
def reliability_start(
    claim: str,
    required_checks: list[str],
    subject: str = "",
    mode: str = "reliability",
    required_claims: list[str] | None = None,
    required_properties: list[str] | None = None,
    corrective_budget: int = 1,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Start a project reliability ledger with explicit required checks."""
    return reliability.start_run(
        HarnessPaths.from_env().root,
        claim=claim,
        required_checks=required_checks,
        subject=subject,
        mode=mode,
        required_claims=required_claims,
        required_properties=required_properties,
        corrective_budget=corrective_budget,
        name=name,
        session_id=session_id,
    )


@mcp.tool()
def reliability_record_check(
    run_id: str,
    check_name: str,
    status: str,
    command: str = "",
    evidence: str = "",
    required: bool = True,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Record one observed check result in an active reliability run."""
    return reliability.record_check(
        HarnessPaths.from_env().root,
        run_id=run_id,
        check_name=check_name,
        status=status,
        command=command,
        evidence=evidence,
        required=required,
        name=name,
        session_id=session_id,
    )


@mcp.tool()
def reliability_record_claim(
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
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Record a structured claim. VERIFIED/REFUTED without a machine receipt is downgraded to INCONCLUSIVE."""
    return reliability.record_claim(
        HarnessPaths.from_env().root, run_id=run_id, claim_id=claim_id, subject=subject,
        predicate=predicate, value=value, status=status, repo_id=repo_id, required=required,
        evidence_ids=evidence_ids, depends_on=depends_on, negates=negates, reason=reason,
        name=name, session_id=session_id,
    )


@mcp.tool()
def reliability_record_assessment(
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
    """Record a flexible claim/hypothesis/observation/gap node with strict provenance and bounded evidence references; never stores private reasoning or raw tool dumps."""
    return reliability.record_assessment(
        HarnessPaths.from_env().root, run_id=run_id, kind=kind, statement=statement,
        node_id=node_id, status=status, authority=authority, analysis_summary=analysis_summary,
        evidence_refs=evidence_refs, relations=relations, required=required, confidence=confidence,
        requirements=requirements, name=name, session_id=session_id,
    )


@mcp.tool()
def reliability_record_relation(
    run_id: str,
    from_node_id: str,
    relation_type: str,
    to_node_id: str,
    relation_id: str = "",
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Record one first-class epistemic relation after both assessment nodes exist."""
    return reliability.record_relation(
        HarnessPaths.from_env().root, run_id=run_id, from_node_id=from_node_id,
        relation_type=relation_type, to_node_id=to_node_id, relation_id=relation_id,
        name=name, session_id=session_id,
    )


@mcp.tool()
def reliability_consume_corrective_budget(
    run_id: str,
    action: str,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Atomically consume one bounded corrective-action slot before doing the action."""
    return reliability.consume_corrective_budget(
        HarnessPaths.from_env().root, run_id=run_id, action=action, name=name, session_id=session_id,
    )


@mcp.tool()
def reliability_aggregate_verdict(
    reliability_run_id: str = "",
    acceptance_run_id: str = "",
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Compose reliability and acceptance component verdicts without collapsing their scopes."""
    return reliability.aggregate_verdict(
        HarnessPaths.from_env().root, reliability_run_id=reliability_run_id,
        acceptance_run_id=acceptance_run_id, name=name, session_id=session_id,
    )


@mcp.tool()
def reliability_verification_checkpoint(
    run_id: str,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Deterministically verify assessment graph coherence, evidence integrity, explicit backend requirements, and unresolved required gaps before a reliability conclusion."""
    return reliability.verification_checkpoint(
        HarnessPaths.from_env().root, run_id=run_id, name=name, session_id=session_id,
    )


@mcp.tool()
def reliability_verify_code_claim(
    run_id: str,
    claim_id: str,
    claim: str,
    subject: str,
    predicate: str,
    value: Any,
    repo: str = "",
    required: bool = True,
    name: str = "",
    refresh_index: bool = False,
    session_id: str = "",
) -> dict[str, Any]:
    """Run code_validate_claim and attach its deterministic receipt to a structured reliability claim."""
    result = core_code_validate_claim(claim=claim, name=name, repo=repo, refresh_index=refresh_index, session_id=session_id)
    status = claim_graph.status_from_code_verifier(result)
    receipt = claim_graph.verifier_receipt("code_validate_claim", result)
    recorded = reliability.record_claim(
        HarnessPaths.from_env().root, run_id=run_id, claim_id=claim_id, subject=subject, predicate=predicate,
        value=value, status=status, repo_id=str(result.get("repo_id") or repo), required=required,
        reason=str(result.get("reason") or ""), verifier=receipt, name=name, session_id=session_id,
    )
    return {"status": "recorded", "claim_status": status, "verifier_result": result, "run": recorded}


@mcp.tool()
def reliability_verify_semantics_claim(
    run_id: str,
    claim_id: str,
    language: str,
    operation: str,
    inputs: dict[str, Any] | None,
    subject: str,
    predicate: str,
    value: Any,
    repo: str = "",
    required: bool = True,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Run an allow-listed semantics oracle; version-sensitive stdlib results require toolchain alignment before they can verify a target-repo claim."""
    result = core_code_semantics_check(language=language, operation=operation, inputs=inputs, name=name, repo=repo, session_id=session_id)
    status = claim_graph.status_from_semantics_verifier(result)
    reason = str(result.get("applicability") or result.get("reason") or "")
    receipt = claim_graph.verifier_receipt("code_semantics_check", result)
    recorded = reliability.record_claim(
        HarnessPaths.from_env().root, run_id=run_id, claim_id=claim_id, subject=subject, predicate=predicate,
        value=value, status=status, repo_id=str(result.get("repo_id") or repo), required=required,
        reason=reason, verifier=receipt, name=name, session_id=session_id,
    )
    return {"status": "recorded", "claim_status": status, "verifier_result": result, "run": recorded}


@mcp.tool()
def reliability_finish(
    run_id: str,
    requested_status: str = "passed",
    risks: list[str] | None = None,
    name: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Finalize a reliability run; check failures and structured-claim conflicts/refutations are enforced, and ship mode fails closed on unverified required claims."""
    return reliability.finish_run(
        HarnessPaths.from_env().root,
        run_id=run_id,
        requested_status=requested_status,
        risks=risks,
        name=name,
        session_id=session_id,
    )


@mcp.tool()
def reliability_status(run_id: str, name: str = "", session_id: str = "") -> dict[str, Any]:
    """Return the durable state and report path for a reliability run."""
    return reliability.get_run(HarnessPaths.from_env().root, run_id=run_id, name=name, session_id=session_id)


@mcp.tool()
def project_index_preview(name: str = "", include_artifacts: bool = True, include_code: bool = False, session_id: str = "") -> dict[str, Any]:
    """Preview exactly what project material would be included or excluded from indexing."""
    project_id = name or None
    return core_project_index_preview(project_id=project_id, include_artifacts=include_artifacts, include_code=include_code, session_id=session_id)


@mcp.tool()
def project_create(name: str, session_id: str = "") -> dict[str, Any]:
    """Create a project workspace under workspace/projects/<name>/ and attach it for the current session."""
    return core_project_create(name=name, session_id=session_id)


@mcp.tool()
def project_resume(name: str, session_id: str = "") -> dict[str, Any]:
    """Attach an existing project for the current session and return compact SITUATION/HANDOFF context."""
    return core_project_resume(name=name, session_id=session_id)


@mcp.tool()
def project_status(name: str = "", session_id: str = "") -> dict[str, Any]:
    """Return attached-project state, continuity generations, index freshness, warnings, and optional pending facets."""
    return core_project_status(name=name, session_id=session_id)


@mcp.tool()
def project_handoff(name: str) -> dict[str, Any]:
    """Refresh project SITUATION.md and HANDOFF.md and return compact handoff metadata."""
    return core_project_handoff(name=name)


@mcp.tool()
def project_migrate(name: str, apply: bool = False) -> dict[str, Any]:
    """Preview or non-destructively migrate typed project JSONL records into the canonical continuity journal."""
    return core_project_migrate(name=name, apply=apply)


@mcp.tool()
def project_list() -> list[dict[str, Any]]:
    """List known project workspaces under workspace/projects/."""
    return core_project_list()


@mcp.tool()
def project_note(name: str, text: str) -> dict[str, Any]:
    """Append free-form notes to workspace/projects/<name>/notes/thoughts.md and refresh handoff."""
    return core_project_note(name=name, text=text)


@mcp.tool()
def project_pending(name: str, title: str, next_action: str, reason: str = "", related_files: list[str] | None = None) -> dict[str, Any]:
    """Compatibility helper: add an optional possible-continuation facet. Projects do not require pending items."""
    return core_project_pending(name=name, title=title, next_action=next_action, reason=reason, related_files=related_files)


@mcp.tool()
def project_mark_pending(name: str, pending_id: str = "", status: str = "done", note: str = "") -> dict[str, Any]:
    """Mark the current/selected project pending item done, blocked, superseded, or continued."""
    return core_project_mark_pending(name=name, pending_id=pending_id, status=status, note=note)


@mcp.tool()
def project_index(name: str, include_artifacts: bool = True, include_code: bool = False, include_qdrant: bool = True) -> dict[str, Any]:
    """Index one project workspace into project-local FTS and optional Qdrant vectors."""
    return core_project_index(name=name, include_artifacts=include_artifacts, include_code=include_code, include_qdrant=include_qdrant)




@mcp.tool()
def burp_run_list(project: str = "", limit: int = 0, all: bool = False) -> list[dict[str, Any]]:
    """List Burp runs. Defaults to global/latest ordering; pass project to filter by project prefix."""
    return burp.burp_run_list(project_related=project, limit=limit, all_runs=all)


@mcp.tool()
def burp_run_summary(run_id: str = "", run_dir: str = "", preview: int = 10) -> dict[str, Any]:
    """Return compact Burp run summary and request previews. Does not dump raw MCP JSON."""
    return burp.burp_run_summary(run_id_value=run_id, run_dir=run_dir, preview=preview)


@mcp.tool()
def burp_find_request(pattern: str, project: str = "", run_id: str = "", run_dir: str = "", all: bool = False, limit_runs: int = 0, max_matches: int = 10) -> dict[str, Any]:
    """Find requests with no fixed run limit by default. Set limit_runs for a bounded preview."""
    return burp.burp_find_request(pattern=pattern, project_related=project, run_id_value=run_id, run_dir=run_dir, all_runs=all, limit_runs=limit_runs, max_matches=max_matches)


@mcp.tool()
def burp_show_request(request_ref: str = "", pattern: str = "", project: str = "", run_id: str = "", run_dir: str = "", all: bool = False, limit_runs: int = 0, max_bytes: int = 6000) -> dict[str, Any]:
    """Show a redacted request preview from a Burp run. Prefer this over manual raw JSON parsing."""
    return burp.burp_show_request(request_ref_value=request_ref, pattern=pattern, project_related=project, run_id_value=run_id, run_dir=run_dir, all_runs=all, limit_runs=limit_runs, max_bytes=max_bytes)


@mcp.tool()
def burp_extract_request(pattern: str = "", request_ref: str = "", burp_id: str = "", project: str = "", run_id: str = "", run_dir: str = "", name: str = "", all: bool = False, limit_runs: int = 0) -> dict[str, Any]:
    """Extract a selected Burp request to a deliberate .http artifact. Raw full history remains global."""
    return burp.extract_request(pattern=pattern, request_ref_value=request_ref, burp_id=burp_id, project_related=project, run_id_value=run_id, run_dir=run_dir, name=name, all_runs=all, limit_runs=limit_runs)


@mcp.tool()
def burp_request_to_repeater(request_ref: str = "", pattern: str = "", project: str = "", run_id: str = "", run_dir: str = "", tab_name: str = "", all: bool = False, limit_runs: int = 0) -> dict[str, Any]:
    """Create a Burp Repeater tab from a selected request. Requires explicit user intent."""
    return burp.burp_request_to_repeater(request_ref_value=request_ref, pattern=pattern, project_related=project, run_id_value=run_id, run_dir=run_dir, tab_name=tab_name, all_runs=all, limit_runs=limit_runs)


@mcp.tool()
def burp_request_to_intruder(request_ref: str = "", pattern: str = "", project: str = "", run_id: str = "", run_dir: str = "", tab_name: str = "", all: bool = False, limit_runs: int = 0) -> dict[str, Any]:
    """Send a selected Burp request to Intruder. Requires explicit user intent."""
    return burp.burp_request_to_intruder(request_ref_value=request_ref, pattern=pattern, project_related=project, run_id_value=run_id, run_dir=run_dir, tab_name=tab_name, all_runs=all, limit_runs=limit_runs)




@mcp.tool()
def burp_host_report(hostname: str, project: str = "", include_global: bool = True, all_projects: bool = False, run_limit: int = 0, max_items: int = 100, write_artifacts: bool = True) -> dict[str, Any]:
    """Build a compact host-focused Burp report from saved inventories. Uses no fixed run limit by default."""
    return burp.burp_host_report(hostname=hostname, project_related=project, include_global=include_global, all_projects=all_projects, run_limit=run_limit, max_items=max_items, write_artifacts=write_artifacts)


@mcp.tool()
def burp_record_observation(project: str = "", title: str = "", summary: str = "", host: str = "", method: str = "", path: str = "", status_code: str = "", request_ref: str = "", artifact: str = "", next_action: str = "", source: str = "burp_mcp", tags: list[str] | None = None) -> dict[str, Any]:
    """Record a compact RAG-safe observation after direct Burp MCP live work. Does not parse/pull raw history."""
    return burp.burp_record_observation(project=project, title=title, summary=summary, host=host, method=method, path=path, status_code=status_code, request_ref_value=request_ref, artifact=artifact, next_action=next_action, source=source, tags=tags)


@mcp.tool()
def burp_save_host_summary(project: str = "", hostname: str = "", summary: str = "", coverage: dict[str, Any] | None = None, request_refs: list[str] | None = None, next_action: str = "", source: str = "burp_mcp") -> dict[str, Any]:
    """Save a compact host/domain summary from direct Burp MCP work into project artifacts/handoff/RAG-safe files."""
    return burp.burp_save_host_summary(project=project, hostname=hostname, summary=summary, coverage=coverage, request_refs=request_refs, next_action=next_action, source=source)


@mcp.tool()
def burp_task_checkpoint(project: str = "", title: str = "", status: str = "running", current_step: str = "", completed_steps: list[str] | None = None, remaining_steps: list[str] | None = None, next_action: str = "", last_tool_output_summary: str = "", related_refs: list[str] | None = None, task_id: str = "") -> dict[str, Any]:
    """Checkpoint live Burp workflow work only. For generic code/docs/research tasks use project_task_checkpoint."""
    return burp.burp_task_checkpoint(project=project, title=title, status=status, current_step=current_step, completed_steps=completed_steps, remaining_steps=remaining_steps, next_action=next_action, last_tool_output_summary=last_tool_output_summary, related_refs=related_refs, task_id=task_id)


@mcp.tool()
def burp_task_status(project: str = "", task_id: str = "", latest: bool = True) -> dict[str, Any]:
    """Return a Burp-workflow checkpoint only. Generic project tasks use project_task_status."""
    return burp.burp_task_status(project=project, task_id=task_id, latest=latest)


@mcp.tool()
def burp_task_finalize(project: str = "", task_id: str = "", outcome: str = "", finding: str = "", next_action: str = "") -> dict[str, Any]:
    """Finalize a Burp-workflow checkpoint only. Generic project tasks use project_task_finalize."""
    return burp.burp_task_finalize(project=project, task_id=task_id, outcome=outcome, finding=finding, next_action=next_action)


@mcp.tool()
def retrieval_status() -> dict[str, Any]:
    """Return passive retrieval configuration and last-known health; performs no network I/O."""
    result = rag_backend.retrieval_status_snapshot()
    result["qdrant_code_collection"] = code_collection_name()
    return result



@mcp.tool()
def retrieval_probe(
    probe_qdrant: bool = True,
    probe_embedding: bool = False,
    probe_reranker: bool = False,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Explicitly probe retrieval backends with bounded network calls and synthetic text only."""
    result = rag_backend.probe_retrieval(
        probe_qdrant=probe_qdrant,
        probe_embedding=probe_embedding,
        probe_reranker=probe_reranker,
        timeout_seconds=timeout_seconds,
    )
    result.update({
        "qdrant_collection": rag_backend.qdrant_collection_name(),
        "qdrant_code_collection": code_collection_name(),
    })
    return result




@mcp.tool()
def recall_context(query: str, include_global: bool = True, limit: int = 10) -> dict[str, Any]:
    """Recall project-first context, optional global context, and matching skills for a task/query."""
    return core_recall_context(query=query, include_global=include_global, limit=limit)


@mcp.tool()
def index_project(include_artifacts: bool = False, include_code: bool = False, include_qdrant: bool = True, project_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Refresh the session-attached project index, or a named project when project_id is provided."""
    return core_index_project(include_artifacts=include_artifacts, include_code=include_code, include_qdrant=include_qdrant, project_id=project_id or None, session_id=session_id)


@mcp.tool()
def index_global(include_qdrant: bool = True) -> dict[str, Any]:
    """Refresh global memory/skill indexes in SQLite FTS and optionally Qdrant/BGE vectors."""
    return core_index_global(include_qdrant=include_qdrant)


@mcp.tool()
def index_all(include_artifacts: bool = False, include_code: bool = False, include_qdrant: bool = True) -> dict[str, Any]:
    """Refresh project and global indexes; optionally include artifacts/code and Qdrant."""
    return core_index_all(include_artifacts=include_artifacts, include_code=include_code, include_qdrant=include_qdrant)


@mcp.tool()
def search_rag(query: str, scope: str = "project", include_global: bool = False, limit: int = 10, session_id: str = "") -> dict[str, Any]:
    """Compatibility search with session-scoped project retrieval and clearly separated global results."""
    return core_search_rag(query=query, scope=scope, include_global=include_global, limit=limit, session_id=session_id)


@mcp.tool()
def search_project_memory(query: str, limit: int = 10, include_sensitive: bool = False, session_id: str = "") -> list[dict[str, Any]]:
    """Compatibility search over the project attached to this OpenCode session."""
    paths = HarnessPaths.from_env()
    records = project_records(paths, session_id=session_id or None)
    if not include_sensitive:
        records = [r for r in records if str(r.get("index_policy") or "safe") != "no_rag" and str(r.get("sensitivity") or "project") not in {"sensitive", "secret"}]
    return search_records(query, records, limit=limit)


@mcp.tool()
def search_global_memory(query: str, limit: int = 10, include_sensitive: bool = False) -> list[dict[str, Any]]:
    """Search global reusable memory; include explicit sensitive records only when requested."""
    paths = HarnessPaths.from_env()
    return search_records(query, global_records(paths, include_sensitive=include_sensitive), limit=limit)


@mcp.tool()
def save_project_fact(text: str, evidence: str = "", tags: list[str] | None = None, confidence: str = "observed", sensitivity: str = "normal", allow_sensitive_plaintext: bool = False, session_id: str = "") -> dict[str, Any]:
    """Save a project-local fact with normal redaction or explicit sensitive handling."""
    return core_save_project_fact(text=text, evidence=evidence, tags=tags, confidence=confidence, sensitivity=sensitivity, allow_sensitive_plaintext=allow_sensitive_plaintext, session_id=session_id)


@mcp.tool()
def save_global_fact(text: str, reason: str = "", tags: list[str] | None = None, allow_sensitive_plaintext: bool = False) -> dict[str, Any]:
    """Save a reviewed global fact. Explicit sensitive values require allow_sensitive_plaintext=true and remain outside automatic indexes."""
    return core_save_global_fact(text=text, reason=reason, tags=tags, allow_sensitive_plaintext=allow_sensitive_plaintext)


@mcp.tool()
def save_finding(title: str, evidence: str, confidence: str = "hypothesis", tags: list[str] | None = None, session_id: str = "") -> dict[str, Any]:
    """Save a structured project finding with evidence and confidence."""
    return core_save_finding(title=title, evidence=evidence, confidence=confidence, tags=tags, session_id=session_id)


@mcp.tool()
def save_hypothesis(hypothesis: str, evidence: str = "", status: str = "open", tags: list[str] | None = None, session_id: str = "") -> dict[str, Any]:
    """Save a project-local hypothesis."""
    return core_save_hypothesis(hypothesis=hypothesis, evidence=evidence, status=status, tags=tags, session_id=session_id)


@mcp.tool()
def classify_memory(text: str) -> dict[str, Any]:
    """Classify text as project, global candidate, hybrid candidate, or secret-reference material."""
    return classify_memory_text(text)


@mcp.tool()
def propose_promotion(memory_text: str, generalized_text: str = "", reason: str = "") -> dict[str, Any]:
    """Queue a local memory for reviewed promotion to global memory."""
    return core_propose_promotion(memory_text=memory_text, generalized_text=generalized_text, reason=reason)


@mcp.tool()
def list_promotion_candidates() -> list[dict[str, Any]]:
    """List pending local-to-global promotion candidates."""
    return core_list_promotion_candidates()


@mcp.tool()
def approve_promotion(candidate_line: int | None = None, generalized_text: str | None = None) -> dict[str, Any]:
    """Approve a promotion candidate and save the generalized lesson globally."""
    return core_approve_promotion(candidate_line=candidate_line, generalized_text=generalized_text)


@mcp.tool()
def reject_promotion(candidate_line: int | None = None, reason: str = "") -> dict[str, Any]:
    """Record rejection of a promotion candidate."""
    return core_reject_promotion(candidate_line=candidate_line, reason=reason)


@mcp.tool()
def demote_global_memory(global_line: int, project_text: str = "", reason: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    """Move a global memory into project scope and hide it from future global recall."""
    return core_demote_global_memory(global_line=global_line, project_text=project_text, reason=reason, tags=tags)


@mcp.tool()
def search_skills(query: str = "", scope: str = "project_first", limit: int = 10) -> list[dict[str, Any]]:
    """Search project and global skills without loading full skill bodies."""
    return core_search_skills(query=query, scope=scope, limit=limit)


@mcp.tool()
def load_skill(name: str, scope: str | None = None) -> dict[str, Any]:
    """Load a skill's full SKILL.md content by name."""
    return core_load_skill(name=name, scope=scope)


@mcp.tool()
def propose_skill_update(skill_name: str, proposed_change: str, reason: str = "", scope: str = "project") -> dict[str, Any]:
    """Queue a reviewed skill update proposal. Does not edit or enable SKILL.md automatically."""
    return core_propose_skill_update(skill_name=skill_name, proposed_change=proposed_change, reason=reason, scope=scope)


@mcp.tool()
def search_evidence(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Legacy bounded artifact/doc discovery hint. Prefer project_search for indexed project evidence."""
    return core_search_evidence(query=query, limit=limit)



@mcp.tool()
def codebase_search(
    query: str,
    name: str = "",
    limit: int = 10,
    refresh_index: bool = False,
    mode: str = "auto",
    view: str = "context",
    use_fts: bool = True,
    use_qdrant: bool = True,
    use_reranker: bool = True,
    result_focus: str = "auto",
    structural_promotion: bool = True,
    strict_backends: bool = False,
    max_chars: int = 0,
    repo: str = "",
    source_id: str = "",
    diagnostic_targets: list[str] | None = None,
    capture_evidence: bool = False,
    acceptance_run_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Repository search with deterministic routing, provenance, and bounded context.

    Normal conceptual search uses the best available FTS/Qdrant/reranker path
    followed by bounded structural candidate expansion, authority-aware ranking,
    and diversity. ``mode=lexical`` plus the ``use_*`` flags exist for explicit
    diagnostics/A-B testing; ``strict_backends`` makes a requested semantic
    backend fail closed rather than silently degrading. ``result_focus=auto`` is
    the normal user-facing mode; explicit implementation/tests/config/balanced
    focus is available for deterministic evaluation. ``view=diagnostics``
    returns telemetry first and compact final hits while storing the complete
    metadata-only candidate trace behind a bounded trace handle. Pass
    ``diagnostic_targets`` to inline exact deep-candidate records, or use
    ``code_diagnostics_trace`` for paged/targeted trace reads. Source previews
    are never stored in diagnostic traces. With ``capture_evidence=true``, Awoki
    stores the returned search evidence plus any metadata-only deep trace as a
    content-addressed project-local non-RAG artifact and returns ``evidence_ref``.
    """
    return core_codebase_search(
        query=query,
        name=name,
        limit=limit,
        refresh_index=refresh_index,
        mode=mode,
        view=view,
        use_fts=use_fts,
        use_qdrant=use_qdrant,
        use_reranker=use_reranker,
        result_focus=result_focus,
        structural_promotion=structural_promotion,
        strict_backends=strict_backends,
        max_chars=max_chars,
        repo=repo,
        source_id=source_id,
        diagnostic_targets=diagnostic_targets,
        capture_evidence=capture_evidence, acceptance_run_id=acceptance_run_id,
        session_id=session_id,
    )


@mcp.tool()
def code_diagnostics_trace(
    trace_id: str,
    name: str = "",
    offset: int = 0,
    limit: int = 25,
    target: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Read a bounded page or exact target from a stored code-search diagnostic trace.

    Diagnostic traces contain ranking/retrieval metadata only, never source
    previews. ``target`` matches candidate path or symbol; otherwise ``offset``
    and ``limit`` page through the bounded pool. Trace access is project-scoped.
    """
    return core_code_diagnostics_trace(
        trace_id=trace_id,
        name=name,
        offset=offset,
        limit=limit,
        target=target,
        session_id=session_id,
    )


@mcp.tool()
def code_index_status(name: str = "", repo: str = "", source_id: str = "", session_id: str = "") -> dict[str, Any]:
    """Report structural code-index freshness, source/revision identity, parser runtime, counts, and vector state."""
    return core_code_index_status(name=name, repo=repo, source_id=source_id, session_id=session_id)


@mcp.tool()
def code_index_verify(
    name: str = "",
    include_qdrant: bool = True,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Explicitly deep-verify repository source freshness and optional code-Qdrant reachability."""
    return core_code_index_verify(
        name=name, include_qdrant=include_qdrant, repo=repo, source_id=source_id, session_id=session_id
    )


@mcp.tool()
def code_definition(
    symbol: str,
    name: str = "",
    view: str = "context",
    limit: int = 10,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Resolve exact symbol definitions without semantic guessing."""
    return core_code_definition(
        symbol=symbol, name=name, view=view, limit=limit,
        refresh_index=refresh_index, repo=repo, source_id=source_id, session_id=session_id,
    )


@mcp.tool()
def code_callers(
    symbol: str,
    name: str = "",
    view: str = "context",
    limit: int = 20,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Return statically resolved callers and explicitly labeled ambiguous or unresolved call sites."""
    return core_code_callers(
        symbol=symbol, name=name, view=view, limit=limit,
        refresh_index=refresh_index, repo=repo, source_id=source_id, session_id=session_id,
    )


@mcp.tool()
def code_callees(
    symbol: str,
    name: str = "",
    view: str = "context",
    limit: int = 20,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Return statically resolved callees and explicitly labeled ambiguous or unresolved calls."""
    return core_code_callees(
        symbol=symbol, name=name, view=view, limit=limit,
        refresh_index=refresh_index, repo=repo, source_id=source_id, session_id=session_id,
    )


@mcp.tool()
def code_path(
    source: str,
    target: str,
    name: str = "",
    view: str = "context",
    limit: int = 30,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Trace a bounded statically resolved function path between exact source and target symbols."""
    return core_code_path(
        source=source, target=target, name=name, view=view, limit=limit,
        refresh_index=refresh_index, repo=repo, source_id=source_id, session_id=session_id,
    )


@mcp.tool()
def code_flow_graph(
    symbol: str,
    name: str = "",
    max_depth: int = 5,
    max_nodes: int = 120,
    max_edges: int = 400,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Build a bounded static flow graph from one exact symbol; traverse only resolved calls and retain ambiguous/unresolved boundaries."""
    return core_code_flow_graph(
        symbol=symbol,
        name=name,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        refresh_index=refresh_index,
        repo=repo,
        source_id=source_id,
        session_id=session_id,
    )


@mcp.tool()
def code_source_window(
    path: str,
    name: str = "",
    start_line: int = 1,
    end_line: int = 0,
    max_chars: int = 20000,
    max_line_chars: int = 4096,
    refresh_index: bool = False,
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Read a bounded hash-checked source range from the active structural index; giant lines are explicitly clipped."""
    return core_code_source_window(
        path=path,
        name=name,
        start_line=start_line,
        end_line=end_line,
        max_chars=max_chars,
        max_line_chars=max_line_chars,
        refresh_index=refresh_index,
        repo=repo,
        source_id=source_id,
        session_id=session_id,
    )


@mcp.tool()
def code_evidence_verify(
    evidence_id: str,
    name: str = "",
    repo: str = "",
    source_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Verify whether a prior code_source_window evidence id still matches the current source and repository snapshot."""
    return core_code_evidence_verify(
        evidence_id=evidence_id, name=name, repo=repo, source_id=source_id, session_id=session_id
    )


@mcp.tool()
def code_semantics_check(
    language: str,
    operation: str,
    inputs: dict[str, Any] | None = None,
    name: str = "",
    repo: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Run a fixed allow-listed Go semantics probe without executing repository code.

    Operations: path_join, path_clean, parse_duration, duration_multiply,
    failed_error_type_assertion, strings_replace, url_parse, and
    reverse_proxy_rewrite_headers.
    """
    return core_code_semantics_check(
        language=language,
        operation=operation,
        inputs=inputs,
        name=name,
        repo=repo,
        session_id=session_id,
    )


@mcp.tool()
def code_exact_search(
    patterns: list[str],
    name: str = "",
    repo: str = "",
    mode: str = "matches",
    paths: list[str] | None = None,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    ignore_case: bool = False,
    fixed_strings: bool = False,
    hidden: bool = False,
    include_ignored: bool = False,
    context_before: int = 0,
    context_after: int = 0,
    offset: int = 0,
    limit: int = 200,
    timeout_seconds: float = 20.0,
    session_id: str = "",
) -> dict[str, Any]:
    """Structured ripgrep exact search without Bash.

    Use for multiple exact/regex expressions, exhaustive file/count enumeration,
    context lines, precise include/exclude globs, and hidden/ignored-file policy.
    This is repository-scoped exact search, not semantic retrieval.
    """
    return core_code_exact_search(
        patterns=patterns,
        name=name,
        repo=repo,
        mode=mode,
        paths_filter=paths or [],
        include_globs=include_globs or [],
        exclude_globs=exclude_globs or [],
        ignore_case=ignore_case,
        fixed_strings=fixed_strings,
        hidden=hidden,
        include_ignored=include_ignored,
        context_before=context_before,
        context_after=context_after,
        offset=offset,
        limit=limit,
        timeout_seconds=timeout_seconds,
        session_id=session_id,
    )


@mcp.tool()
def code_text_search(
    pattern: str,
    name: str = "",
    paths: list[str] | None = None,
    page_size: int = 1000,
    cursor: str = "",
    preview_chars: int = 320,
    ignore_case: bool = False,
    fixed_string: bool = False,
    include_ignored: bool = False,
    shard_timeout_seconds: float = 15.0,
    operation_timeout_seconds: float = 20.0,
    repo: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Exhaustive repository-source text fallback with source-aware secret handling and materialized resumable transport."""
    return core_code_text_search(
        pattern=pattern,
        name=name,
        paths_filter=paths or [],
        page_size=page_size,
        cursor=cursor,
        preview_chars=preview_chars,
        ignore_case=ignore_case,
        fixed_string=fixed_string,
        include_ignored=include_ignored,
        shard_timeout_seconds=shard_timeout_seconds,
        operation_timeout_seconds=operation_timeout_seconds,
        repo=repo,
        session_id=session_id,
    )


@mcp.tool()
def cross_project_code_search(
    query: str,
    projects: list[str] | None = None,
    all_indexed: bool = False,
    mode: str = "auto",
    view: str = "context",
    limit: int = 20,
    refresh_stale: bool = False,
) -> dict[str, Any]:
    """Search code across an explicit project list, or all indexed projects only when all_indexed=true."""
    return core_cross_project_code_search(
        query=query, projects=projects, all_indexed=all_indexed,
        mode=mode, view=view, limit=limit, refresh_stale=refresh_stale,
    )


@mcp.tool()
def code_validate_claim(
    claim: str,
    name: str = "",
    refresh_index: bool = False,
    repo: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Validate one atomic code claim using exact symbols, AST/control flow, and resolved graph edges only; broad requests must be decomposed by the client; no embeddings or reranking."""
    return core_code_validate_claim(
        claim=claim, name=name, refresh_index=refresh_index, repo=repo, session_id=session_id
    )


@mcp.tool()
def code_evaluate(suite: str = "smoke", report_name: str = "") -> dict[str, Any]:
    """Run a versioned structural code-search golden-query suite and write a deterministic JSON report."""
    return core_code_evaluate(suite=suite, report_name=report_name)


@mcp.tool()
def open_artifact(path: str, max_bytes: int = 20000, redact_secrets: bool = True) -> dict[str, Any]:
    """Open a text artifact under the harness root with path traversal protection and secret redaction by default."""
    return core_open_artifact(path=path, max_bytes=max_bytes, redact_secrets=redact_secrets)


if __name__ == "__main__":
    # Do not log to stdout; stdout is reserved for MCP stdio.
    mcp.run()
