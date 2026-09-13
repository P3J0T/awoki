---
name: project-continuity
description: Create, resume, search, capture, correct, checkpoint an investigation, refresh, pause, or switch Awoki project work without requiring formal tasks or pending items.
compatibility: opencode
metadata:
  scope: project
  tags: project,continuity,resume,memory,handoff,natural-language
---

# Awoki Project Continuity

## Core behavior

The user drives direction. Do not force goals, task lists, pending items, scope
forms, or Burp state onto a project.

Use the continuity-first path:

```text
workspace activity -> concise project_capture -> generated views -> targeted search
```

For multi-step natural-language investigations, use the native OpenCode TODO list as
the small active working set: summarize the user's requested outcomes/constraints in
roughly 3–8 bounded items, update it when direction changes, and avoid copying the raw
prompt or every intermediate thought. Awoki mirrors that projection outside chat so it
survives compaction; do not create a separate session-intent object just to restate it.

Never store private chain-of-thought. Store only concise operational continuity
supported by observable work and source references.

## Natural-language routing

`/project` is the broad user-facing entry point for these operations. Ordinary natural language is equally valid.


- “Create/open/resume/switch to PROJECT” -> `project_open`
- “Add/register repo REPO” -> `project_repo_add`; when no path is supplied, infer `repo/REPO`
- “List/show repos” -> `project_repo_list`
- “Remove repo REPO” -> `project_repo_remove`; this removes registration only, never repository files
- “Use REPO as the default repo” -> `project_repo_default`
- “Remember/save/note that …” -> `project_capture` with the neutral default `kind="observation"` unless the user explicitly names another kind
- “This is a project fact …” -> `project_capture` with `kind="fact"`; do not require evidence merely to preserve a user-supplied project fact
- “We found/discovered …” after an investigation -> `project_capture` with `kind="finding"` or `kind="discovery"`; attach concrete sources when available
- “We decided …” -> `project_capture` with `kind="decision"`
- “That earlier assumption was wrong” -> capture a `correction` with `supersedes`
- “What do we know about …?” -> `project_search`
- “Search/ask the codebase …” or `/codebase` -> `codebase_search`; infer `peek`, `context`, or `full` from the wording
- “Where is SYMBOL defined?” -> `code_definition`
- “Who calls SYMBOL?” / “What does SYMBOL call?” -> `code_callers` / `code_callees`
- “Trace SOURCE to TARGET” -> `code_path`
- broad “explain/trace how this input/file/request/tree is processed” -> `codebase_search`, resolve one entry point, then `code_flow_graph` + `code_source_window`; use `code_validate_claim` selectively for supported atomic claims
- “Search PROJECTS for …” -> `cross_project_code_search` with 1–8 exact project IDs and native user approval; `all_indexed=true` is rejected. Multiple repos inside ONE project use `codebase_search`.
- “Verify/validate this exact atomic code claim …” -> `code_validate_claim`
- broad “verify this execution/tree/file-processing logic …” -> discover relevant source, decompose it into exact obligations, then call `code_validate_claim` per supported obligation
- “Update the handoff/index/snapshot” -> `project_refresh`
- “Checkpoint this investigation / save where we are / resume this investigation” -> the lightweight investigation checkpoint procedure below; no formal task is required
- “Pause/save a whole-project handoff” -> `project_pause`
- “Checkpoint/status/finalize this tracked long-running task” -> `project_task_checkpoint` / `project_task_status` / `project_task_finalize` only for an existing task identity; Burp task tools are Burp-only
- “What project is active/is the index fresh?” -> `project_status`
- “Prepare/prime/warm this repo for code review/full retrieval” -> load the `repository-readiness` skill; it starts/adopts one durable project/repo/source-scoped `repository_prepare_*` parent job, projects progress into OpenCode TODO, and treats conversation continuation as optional best-effort UX without weakening remote-upload consent

Use explicit `name` when the user names a project. Otherwise rely only on the
OpenCode-session attachment injected by the Awoki plugin. Never silently fall back
to legacy memory. An explicit existing project may be operated on while the session
is unattached; do not attach it merely for background readiness. The durable parent job
is independent of session attachment. If a different project is attached, optional
conversation continuation for another project waits rather than auto-switching the
user's active scope.

## Lightweight investigation checkpoint

Use this for an explicit checkpoint request, a meaningful discovery/correction,
closing or changing an investigation branch, and before a planned pause or
compaction. Do not save after every tool call or manufacture a checkpoint when
nothing changed. Automatic compaction may interrupt before one can be written:
capture important findings as they occur, not only at the end. This is an ordinary
continuity procedure, not a formal acceptance gate or a new memory store.

The user chooses the question, priorities and consequential actions. The model
inspects evidence and drafts/saves concise records; do not ask the user to fill in
a form or approve every note. Ask about material ambiguity or new authority.

1. Reconcile the active question/scope with the newest user direction. Keep the
   next few actions in native TODOs; they are temporary, not established knowledge.
2. Capture useful observations individually (`project_capture`, optionally
   `items=[...]`). Separate observed behavior from inference. Put returned
   `evidence_ref` values in `sources`, and missing checks/dynamic boundaries in
   `uncertainty` **at initial capture**. A citation, high confidence or current
   source does not establish that the interpretation is true. If an important
   check was not performed, say so; do not invent evidence to complete the note.
3. Keep leads explicit: `unresolved`, `supported`, or `rejected`, with a short
   evidence-based reason. To retire a saved lead, append `kind="correction"`,
   `supersedes=[lead_id]`, `state="closed"`, and a summary starting `Rejected lead:`.
   Retain the supporting sources and remaining caveats; leave its next action
   empty. A supported lead needs an evidence-backed finding/correction, not a
   relabelled guess. Reopen a rejected lead only on new evidence or user direction.
4. Save one short orientation note using ordinary `project_capture` with
   `kind="reflection"`, `tags=["investigation-checkpoint", "<topic>"]`, and a
   recognizable summary such as `Checkpoint: <topic> — <bounded current position>`.
   Aim for roughly 150–250 words, fewer for small tasks. Adapt/omit empty sections:

   ```text
   Question/scope: what we are investigating, which sources/revisions apply.
   Established: narrow observations/findings and exact saved record/source refs.
   Unknown/contradicted: missing checks, external boundaries, conflicting evidence.
   Leads: unresolved/supported/rejected, with brief reasons and record refs.
   Next check: the smallest useful check and what it would resolve.
   ```

   Use `details` for that outline, `uncertainty` for its load-bearing caveats and
   `likely_continuation` for the next check. When deriving it from saved notes,
   use `based_on=[cont_...]` for up to three relevant active safe records. Include
   other exact record pointers in details and disclose omitted scope; never claim
   automatic inheritance beyond those parents. Do not recursively summarize old
   checkpoints instead of reading current findings/corrections. Checkpoints are
   dated snapshots, not live dependencies: a parent correction does not update
   existing snapshots. If an old checkpoint now misleads, explicitly correct it
   with `supersedes`, carrying still-valid sources/caveats; do not combine that
   operation with `based_on`. Preserve the append-only history.
5. Inspect capture results (including partial/rejected items). Do not say “saved”
   without an observed saved ID. Refresh generated views only when needed;
   `project_pause` is for an actual pause/detach, not every checkpoint. Never edit
   `SITUATION.md` or `HANDOFF.md` directly or create a parallel checkpoint database.

On resume, open the intended project and use `project_search` for the checkpoint
topic plus related corrections. Exact-read its record and important referenced
notes with `record_ids`; follow pagination rather than trusting a preview. Select
the latest **relevant** checkpoint, not the newest note from an unrelated branch.
Reconcile `session_work_status`/TODOs with the user's current direction. Keep
rejected leads rejected and unknowns unknown. Reopen load-bearing source evidence
before strengthening a consequential conclusion, not every source on every turn.
If notes are missing, stale or ambiguous, disclose the gap and ask or perform the
smallest authorized check; do not reconstruct certainty from compacted chat.

## Compaction-safe operational continuity

OpenCode TODO is a UI projection, not project memory. The continuity plugin mirrors
bounded native TODO state into Awoki's session work ledger automatically. This works
for attached projects and for unattached/ad-hoc sessions. After compaction, or when
exact operational progress is uncertain, call `session_work_status` rather than
reconstructing the old plan from conversational memory. If `todos_need_review=true`,
the mirrored TODOs predate a newer user turn: reconcile them with the newest user
instruction before acting or rewriting TODOs. Never silently restore an older mirrored
TODO list over newer native OpenCode state.

For an acceptance/benchmark sequence whose final result depends on exact observations
from multiple tool calls, do not rely on the chat transcript surviving compaction. Use
the acceptance ledger as the compact control plane and content-addressed evidence
artifacts as the rich supporting-evidence plane:

1. Call `acceptance_run_start` before Test 1 with explicit managed project/repo/source
   scope and the expected test/invariant IDs when known. If the suite already defines a
   sequence, include a bounded `test_plan`. Besides test id/objective/action labels/stop
   boundary, v4 may encode machine-observable protocol requirements such as
   `required_interfaces`, `required_orchestration_interfaces`, `required_observations`, scalar `pass_requirements`,
   `evidence_scope=current_acceptance_run`, minimum evidence/candidate refs,
   `allowed_native_tools` plus `native_tool_limits`, optional `interface_limits` / `orchestration_interface_limits`, and `forbidden_tool_classes`.
   These are verification/workflow conditions, not an epistemic schema; do not encode
   analytical conclusions there.
2. Call `acceptance_run_next` and execute only the returned unfinished step. Call it
   again immediately after compaction. It returns the exact durable contract plus
   machine-observed structural execution plus orchestration provenance for the current test, so do not recover
   missing PASS criteria from compacted conversation prose. The acceptance ledger does
   not own the reliability corrective budget.
3. For Awoki retrieval whose detailed support may matter later, call `codebase_search`
   with `capture_evidence=true` and the exact `acceptance_run_id`. The returned
   `evidence_capture.evidence_ref` addresses the exact Awoki-produced result; diagnostic
   searches also preserve their complete metadata-only deep candidate trace in that
   artifact so MCP restart/compaction does not strand a process-memory trace handle.
4. Immediately record the test with `acceptance_run_record`. Put only small scalar
   observations in `evidence=`. Keep `notes` short (<=800 characters and <=4 newlines);
   put rich detail in `ev_...` and use `reference_annotate` when a stable object needs a
   human label/`why_saved` instead of copying long context into the ledger. Pass `evidence_refs`, canonical `candidate_ids`, and
   `primary_candidate_id` from the captured evidence rather than reserializing source,
   raw tool output, or candidate-specific rank aliases into the ledger. A claimed PASS
   can be machine-downgraded to `incomplete` or `protocol_deviation` when required
   interfaces/pass conditions/current-run evidence/native-tool rules were not observed.
5. Record cross-test assertions with `acceptance_run_record_invariant`; attach an
   `evidence_ref` when the invariant depends on a captured tool result.
6. After compaction, call `acceptance_run_next` first, then `acceptance_run_status` when
   the full ledger is needed. The run tracks compaction generation/count plus bounded trigger-tagged events across automatic
   and manual compaction. If richer
   prior support is needed, call `acceptance_evidence_get` with the stored stable ref
   and a narrow selector/page; never rerun an expensive retrieval merely because chat
   context was compacted.
7. Build the final report from `acceptance_run_status`, then call
   `acceptance_run_finalize`. Incomplete expected tests/invariants are a precondition
   failure: the run remains `running` and resumable. `not_passed` is terminal only when
   all required evidence exists but one or more recorded outcomes actually fail/violate.
   Source revision or published vector-membership drift also fails closed.

Rich evidence artifacts live project-locally under an `artifacts/acceptance/raw/`
path, are content-addressed/integrity-checked, and are deliberately never registered
for RAG. Stable `ev_` identity remains content-addressed; a bounded non-RAG provenance
sidecar records which acceptance runs captured the artifact, so a current-run evidence
requirement can be enforced without changing the evidence ID. They may contain detailed source/tool evidence, so retrieve them explicitly and
narrowly; the compact acceptance ledger itself remains small and unambiguous.

The acceptance ledger preserves **recorded observations plus evidence references**. It
does not turn a model-authored PASS/HOLD into machine proof; use deterministic
reliability/verifier tools when proof is required.

## Agent-runtime recovery boundary

`session_runtime_status` reports structural reasoning/text/tool terminal-turn metadata.
Check `current_turn_complete`, not a previous terminal event or CLI exit code.
New user turns await their own parent-attributed terminal text response; compaction
summaries, denied/tool-only turns and missing idle events cannot inherit old success.
`reasoning_only_terminal_turn` and `tool_execution_without_followup` are runtime
degradation, not failed evidence. Never
persist private reasoning content and do not consume a reliability corrective budget for
a user-issued follow-up. Generic model turns are detection/reporting only; the detached
job `project_continuation_*` path is the only bounded automatic session-resume mechanism.


Reference navigation:
- `reference_describe(id)` explains what a durable ID is, why it exists, and its bounded links/provenance.
- `reference_annotate(id, label=..., why_saved=..., aliases=[...])` adds human navigation metadata without changing the ID.
- `reference_resolve(query)` maps natural wording to bounded candidate stable IDs; ambiguous/low-confidence wording returns no resolved ID, and resolution never proves the referenced content.
- `reference_describe(cand_...)` distinguishes the candidate's first materialization from later `observed_in` evidence occurrences. `reference_describe(aat_...)` exposes an immutable acceptance attempt; later re-records never erase prior machine outcomes.
Use exact IDs for subsequent evidence/state operations.

For Awoki's own bounded internal acceptance regression, use `harness_self_check` with an
allow-listed check name. It is deliberately not a generic shell runner. Do not substitute
native Bash/Read to rediscover or broaden an allow-listed self-check after compaction.

The mapped names are MCP tools. Invoke them directly through Awoki; do not invent
shell wrappers such as `awoki_project_refresh`. Environment changes made by a Bash
tool call do not alter the already-running MCP server. Normal skills must not source
or print `/run/awoki/runtime.env`; MCP already receives that allowlisted handoff.
When retrieval configuration is in question, call passive `retrieval_status`; use
`retrieval_probe` only when live backend connectivity must be tested. For an explicit
user-requested shell diagnostic, use `make runtime-config`, `make embedding-benchmark`,
`make reranker-benchmark`, or the profile-filtered `awoki-runtime-env` wrapper. Never
run repository-controlled code or downloaded tools under `retrieval`/internal `mcp`/`all`. Profiles
limit inherited variables but are not a same-user sandbox because `op` can read the
stdio MCP runtime snapshot. If target code itself must execute, use a separate
credential-free sandbox. If the effective config differs from intended `.env`,
recreate the service and restart OpenCode before retrying.

## Resume

Call `project_open`. Read the returned bounded situation and handoff before targeted
retrieval. Do not load all files or all RAG hits. Follow the user's new instruction
even when generated continuation suggests something else.

Also inspect `repository_index_advice` returned by `project_open`. When an existing
structural/FTS snapshot is stale, use the exact recommended detached
`code_index_refresh_start` job first. Report its job id and return control; never
autonomously poll it in a loop. Use `code_index_refresh_status` only when the user
asks or when a later requested action genuinely depends on current structural state,
and cancel only on explicit request. The job is local-only and performs no remote
embedding/Qdrant writes. When structural search is current but semantic vectors are
missing/stale, proactively tell the user that conceptual search can fall back to local
FTS and offer the exact recommended `code_vector_refresh_start` background job.
Report its job id and return control; never autonomously poll it in a loop. Use
`code_vector_refresh_status` only when the user asks for status or when a later
requested action genuinely depends on current vector state. Use
`code_vector_refresh_cancel` only when the user asks to stop it. This is an opt-in
remote-indexing action: never invoke it just because the project was opened. The
same advice is returned after `project_repo_add`.
Respect `recommended_poll_after_seconds`/`next_poll_after`; if status returns
`poll_too_soon=true`, report cached progress and return control rather than
calling status again until the retry interval has elapsed or the user explicitly
asks again.

## Repository analysis

`codebase_search` is repository-only and uses Awoki's native structural index. Its
first use explicitly enables safe source indexing for the attached project's
repository registry. Legacy projects use `repo/` as the exact Git root; registered
multi-repo projects use exact child roots such as `repo/oathkeeper`. Broad
`codebase_search` spans enabled child repositories, while exact operations use
`repo=` when more than one repository is registered. Repository understanding is evidence-backed by default: the user
should not need to add "deterministically" to ordinary requests such as "explain
this flow", "trace how input is processed", or "understand this subsystem".

Use indexed discovery first. The deterministic router may select lexical, exact,
definition, callers, callees, path, or conceptual retrieval. Structural/FTS/Qdrant
hits locate candidate code; semantic similarity is never behavioral proof.
For reranker acceptance diagnostics, read the existing `codebase_search` path `details.retrieval.reranker`; do not recursively hunt the response with native Python/grep and do not infer reranker execution from score shapes. When the search was captured as `ev_`, `acceptance_evidence_get(selector="backend_observations.reranker")` returns a bounded derived view without any additional backend request or polling.
Conceptual retrieval keeps tests/config/schema/docs available, but
`result_focus=auto` prefers relevant production implementations for
implementation/runtime/security questions and preserves test/config roles when
the query explicitly asks for them. Bounded structural graph expansion is
candidate generation only: a production target reached from a test/config hit
must still earn relevance against the original query and is not authoritative
merely because an edge exists. When benchmarking retrieval, use real lexical and
per-backend MCP controls plus `strict_backends`; never pretend a backend was
disabled or infer reranker execution from score shapes. For large acceptance
runs use `view=diagnostics`; pass `diagnostic_targets` for named deep candidates
and use `code_diagnostics_trace` to page or target the stored metadata-only trace
instead of Grep/Read against OpenCode's cached tool output. Once an
entry point is known, use exact symbol operations. For flow-oriented questions,
use `code_flow_graph` to build a bounded relevant reachable graph from that entry
point. It traverses only resolved calls and retains ambiguous/unresolved calls as
explicit boundaries. Then use `code_source_window` to inspect bounded,
hash-checked source for control conditions, assignments/aliases, argument passing,
returns, and terminal outcomes. Important windows carry an `evidence_id` that
can be rechecked with `code_evidence_verify` after edits or Git-view changes;
the token detects drift but is not a signature or authorship attestation. A call graph alone does not prove local control or
data flow.

Treat repository assurance separately from answer confidence. `VERIFIED_SNAPSHOT`
means deep index/verify work established a clean exact-root Git snapshot;
`WORKING_TREE_BOUND` means source is still hash-bound but the whole worktree is
not claimed immutable; `FILESYSTEM_BOUND` is intentional non-Git evidence.
Unusual Git state lowers assurance instead of hiding code. Author/committer names
are metadata claims, not verified human identity.

Use `code_validate_claim` selectively for important atomic propositions that its
strict proof profile supports. For broad verification requests, first discover
and inspect the implementation, then decompose the requested behavior into exact
atomic obligations. Do not send vague natural-language requests directly to the
strict MCP primitive merely to obtain `INCONCLUSIVE`. It re-resolves exact symbols
against fresh hashed source, checks supported AST and lexical-scope obligations,
and treats graph edges only as candidate evidence—never as proof by themselves
and never using embeddings or reranking.

For supported Go language/stdlib primitives, use `code_semantics_check` instead
of intuition: path join/clean, duration parsing/multiplication, failed `error`
type assertion, bounded string replacement, URL parsing, and `httputil.ReverseProxy` Rewrite-entry forwarded-header behavior. Docker executes a fixed stdlib-only helper precompiled by the pinned Go builder stage; source-tree development may compile the same fixed helper locally. It never compiles/executes repository code and has no network path. Respect its toolchain-alignment warning for version-sensitive stdlib claims.

Choose exact-search tools by the question being asked. Use `codebase_search` first for
conceptual/architectural discovery. Use OpenCode `Grep` for ordinary known
string/symbol lookup. Use native `rg` through Bash when the full ripgrep CLI makes a
complex or exhaustive exact enumeration materially clearer or cheaper (counts,
multiple expressions, context, precise globs, etc.). Native `rg` is not a forbidden
fallback in normal security/code-review work and does not need an Awoki search to fail
first when the task itself is exact enumeration. If exact-search output truncates,
errors, or cannot establish the coverage required for a claim, use `code_text_search`;
paginate through `next_cursor` until `scan_complete=true` and then
`search_complete=true`, and require `repository_universe_complete=true` before
claiming exhaustive repository-source coverage. `include_ignored=true` remains an
explicit forensic opt-in. Lexical results are discovery only; reopen authoritative
source before asserting behavior. Active acceptance contracts may deliberately forbid
native tools and override this normal-work policy.

Keep continuity reconciliation memory-only; repository chunks are evidence, not
prior memories.

## Capture quality

Keep capture ergonomic. The user normally should not have to classify a record or supply an evidence form.

For a plain “save/remember/note this”, use `kind="observation"`, `confidence="medium"`, and no sources unless useful references are already known. Do not silently promote a generic save into a finding. Do not ask for evidence, confidence, uncertainty, or continuation fields unless the user's wording or the task actually depends on them.

Use stronger labels only when they add meaning:

- `fact`: a project fact the user explicitly wants retained
- `finding` / `discovery`: an investigation result; concrete sources are expected for high-confidence claims
- `decision`: an adopted choice; evidence is optional
- `question` / `hypothesis`: unresolved material
- `correction`: supersedes an earlier record
- `direction`: explicit next direction
- `artifact` / `reflection`: material or operational continuity

A continuity record may also carry details, tags, uncertainty, likely continuation, and sources when those fields are useful. Missing optional fields are not a capture failure.

For several investigation conclusions, prefer `project_capture(items=[...])`:
one scoped observation per item, each with its own `sources=[evidence_ref]` from
`code_source_window` and `uncertainty`. Keep the actual observed condition/outcome
separate from unknown external behavior. This is optional batching of ordinary
records, not a new claim ledger or a required form for casual notes. Batch writes
are independent; inspect each result when the overall status is `partial`.
If capture reports `invalid_sources`, recover the real handle with session/reference
navigation or reopen the source. Never invent, fuzzy-replace or silently drop a
reference to make capture succeed. Existing invalid history needs an explicit
correction; casual unreferenced notes remain valid.
`evidence_refs=[ev_...]` is also accepted and merged into `sources`, both for a
single note and each typed item. Keep privacy settings at the batch top level.
Capture returns `source_binding` (bound/mixed/unbound), never semantic proof.
For a follow-up or summary of saved notes, use `based_on=[cont_...]` (up to three
active safe notes in this project). Awoki retains their sources and uncertainty
and prevents inherited confidence from increasing. Add new caveats/evidence as
needed. This preserves qualifications, not semantic entailment. Missing, private,
retired or invalid-source parents fail closed: recover the actual note instead
of dropping its ID. To resolve a caveat, use a separate explicit `correction`
with `supersedes`, not `based_on`. Similarity only suggests related notes; it
never supplies a parent or evidence binding automatically. Casual independent
notes still need no parent or source form.
Reopen a saved source directly with `code_source_window(evidence_ref=ref)` or the
returned `reopen_call`. Do not prepend the repo name to the repo-relative path.

Search previews explicitly mark omitted material with `complete=false` and
`next_calls`. Read `project_search(record_ids=["cont_..."])` to recover a current
safe note without embedding/reranking or refreshing indexes. Follow returned
section pages for long details, sources or caveats. Never infer that a prior
finding was not saved from a truncated preview. Exact recall is saved knowledge,
not verification: reopen load-bearing source before strengthening the conclusion.

Record IDs (`cont_...`) are durable internal references. Keep them in tool results and stored records because corrections, supersession, deduplication, and diagnostics rely on them; do not require users to manage or quote IDs during ordinary saves.

Do not capture every prompt or transient tool error. Automatic capture handles several observable operations and compaction/pause boundaries.

## Safety

- Never put raw Burp traffic in continuity.
- Normal analysis is coverage-first: high-confidence credential values are redacted best-effort, but auth/security vocabulary and code snippets remain normal retrievable continuity. Redaction alone does not make a record `no_rag`. Only explicit user-directed sensitive plaintext capture remains secret/no-RAG and outside generated views and automatic retrieval.
- Register only safe summaries as indexable artifacts.
- Use `project_index_preview` to inspect inclusions/exclusions.
- Global knowledge must remain clearly labeled and must not override project facts.

## Compatibility

Old fact, finding, hypothesis, pending, handoff, and Burp tools may be used when a legacy workflow requires them. Treat them as adapters into the canonical continuity journal, not alternative project models. Do not recreate removed slash-command aliases for those adapters.
