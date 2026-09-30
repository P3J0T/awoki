## Awoki execution invariants

Project work: `harness_status`, `project_open`, skill. Disclose unavailable
MCP. Save/reuse multi-step goals: `project_capture(kind="direction")` with outcome, constraints,
completion conditions, detail refs. Put its ID early in a native TODO with next actions.
Load `project-continuity` for checkpoints: save evidence, unknowns and next check
as `kind="reflection", tags=["investigation-checkpoint"]`; no formal task needed.
Save meaningful changes. After compaction, use the recovery snapshot or
`session_work_status`; read relevant notes once. Reconcile current user
direction; side questions do not cancel goals. No goal is valid; failed recovery
is unknown.

Awoki names are MCP tools. Discovery: `codebase_search`;
exact lookup: OpenCode Grep with repo path; complex enumeration: `code_exact_search`.
Truncated/completeness-sensitive search needs paginated `code_text_search`.
Reopen source: bounded relationships and hash-checked `code_source_window`,
then supported `code_validate_claim` / `code_semantics_check`. Graphs show possible
flow, tests show contracts; neither proves runtime execution.
Preserve source identity, freshness, ambiguity and toolchain limits.
Copy `citation`/`evidence_ref` exactly; reopen with
`code_source_window(evidence_ref=ref)`, not a guessed path.
Save observations via `project_capture(items=[...])`, `sources=[evidence_ref]`
and `uncertainty`. Follow-ups use `based_on=[cont_id]` to retain sources/caveats;
resolve caveats via correction + supersedes. Keep unknowns; no blanket "secure"
verdicts. Casual notes need no evidence. Previews omit details:
follow `next_calls` / `project_search(record_ids=[cont_id])` before declaring
data missing. Inspect source conditions AND use of results.
`source_freshness` checks bytes/identity, not truth.

The newest user instruction overrides continuation/TODO suggestions; project memory
overrides global memory. Recover by exact ID, not reconstructed summaries.
Never save private reasoning, widen scope silently, send no-RAG/secrets to
retrieval, embed merely opened repositories, or retry/poll indefinitely.
External actions require user authority; workflow labels do not grant it.

## Awoki reliability invariants

Models/memory can err. Verify source/config/runtime/test/tool
claims against observed evidence; question wording is not evidence.
Never claim a check ran unless its result was observed.
Separate observation, inference and hypothesis;
preserve corrections, contradictions, uncertainty and scope. Search hits are
discovery; absence is not universal proof. Use proportionate checks and bounded
correction, not recursive reflection. Exploration can stay incomplete.
Gates cannot turn inference or missing checks into PASS. Disclose failed,
INCONCLUSIVE and unperformed checks. A check-only NOT_APPLICABLE claim gate
does not verify findings. Unresolved external/dynamic callees remain unknown.
`/reliability-check` is local-only; shipping requires explicit authorization.
