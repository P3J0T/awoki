## Awoki execution invariants

Project work: `harness_status`, `project_open`, relevant skill.
Resume via `project_resume`; disclose unavailable MCP.
Investigation checkpoints: load `project-continuity`; use notes, not formal tasks.

Awoki names are MCP tools, not shell commands. Discovery:
`codebase_search`; exact lookup: OpenCode Grep; complex enumeration:
`code_exact_search`. Truncated/completeness-sensitive search needs paginated
`code_text_search` coverage. Reopen source before behavioral claims: bounded
relationships and hash-checked `code_source_window`, then supported
`code_validate_claim` / `code_semantics_check`. Static graphs show possible flow,
tests show contracts, neither proves runtime execution. Preserve source/repo/revision,
freshness, ambiguity and toolchain limits.
Copy `citation`/`evidence_ref` exactly; reopen with
`code_source_window(evidence_ref=ref)`, not a path guessed from the citation.
Save separate observations via `project_capture(items=[...])`, with
`sources=[evidence_ref]` and `uncertainty`. Follow-ups use `based_on=[cont_id]`
to retain sources/caveats; resolve caveats via correction + supersedes.
Keep unknowns, not blanket "secure" verdicts. Casual notes need no evidence.
Previews omit details: follow `next_calls` or `project_search(record_ids=[cont_id])`
before saying a finding was not saved. Reopen source: inspect conditions AND how
results are used. `source_freshness` checks bytes/identity, never claim truth.

The newest user instruction overrides continuation/TODO suggestions.
Project memory overrides global memory. Recover work/evidence by exact ID,
not reconstructed compaction results. Never persist
private reasoning. Never silently widen scope, send no-RAG/secrets to retrieval
endpoints, embed a repository just because it was opened, or retry/poll indefinitely.
External actions require explicit user authority; workflow labels do not grant it.

## Awoki reliability invariants

Model output and remembered conclusions are fallible. Verify concrete source,
configuration, runtime, test and tool-state claims against observed evidence.
Question wording is not evidence; reject unsupported premises.
Never claim a check ran unless its result was observed. Separate observation,
inference and hypothesis; preserve corrections, contradictions, uncertainty and
scope. Search hits are discovery only; absence is not universal proof. Prefer the
smallest adequate check. Exploration may remain incomplete; completion needs
proportionate evidence. Formal gates must not promote inference to verified fact
or missing checks to PASS. Use bounded correction, not recursive reflection.
Final answers must disclose INCONCLUSIVE, failed or unperformed checks. A check-only ledger with
claim gate NOT_APPLICABLE does not verify the findings. Unresolved external/dynamic
callees remain unknown: do not infer their location, trust, intent or lack of authorization.
`/reliability-check` is local-only; shipping requires explicit authorization.
