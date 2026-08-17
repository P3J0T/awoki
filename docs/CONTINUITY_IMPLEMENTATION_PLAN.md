# Awoki Continuity-First Implementation Plan

Status: **Complete**
Governing design: [`CONTINUITY.md`](CONTINUITY.md)
Principle: the user drives direction; Awoki quietly preserves bounded, source-aware continuity.

This document is the authoritative implementation checklist for the continuity-first redesign. A phase is complete only when its acceptance criteria and tests pass. Compatibility code may remain, but it must not weaken the preferred model.

## Release invariants

These invariants apply to every phase and are release-blocking:

- [x] Secrets, raw Burp traffic, credential values, private keys, and `no-rag` material never enter FTS or vector indexes.
- [x] Project-specific writes never silently fall back to global or legacy memory.
- [x] Generated views are deterministic projections, not manually maintained truth stores.
- [x] Automatic continuity capture is source-aware, debounced, idempotent, and never stores hidden reasoning.
- [x] Project attachment is scoped to the OpenCode session and safe under concurrent processes.
- [x] Explicit user direction always overrides suggested continuation.
- [x] Resume returns bounded usable context rather than file paths alone.
- [x] Old data is migrated non-destructively and remains recoverable.
- [x] Integrations emit generic continuity records instead of inventing separate continuity systems.

## Target user workflow

Natural-language interactions should map to one obvious operation:

| User intent | Preferred operation |
|---|---|
| Create, resume, or switch project | `project_open` |
| Remember a fact, finding, decision, correction, question, or artifact | `project_capture` |
| Find project knowledge | `project_search` |
| Rebuild generated views and safe indexes | `project_refresh` |
| Pause or checkpoint | `project_pause` |
| Diagnose attachment, freshness, or exclusions | `project_status` |

Tasks, pending items, Burp state, external sensitive-data skills, repositories, and reports are optional facets and adapters.

## Phase 0 — Freeze behavior and invariants

### Work

- [x] Keep `docs/CONTINUITY.md` as the governing architecture.
- [x] Encode release invariants in unit and end-to-end tests.
- [x] Define the common record envelope and generated-view budgets.
- [x] Document global-vs-project scope and source/confidence rules.

### Acceptance

- No implementation can pass validation while violating a release invariant.
- Tests cover work without goals or pending items, corrections, project switching, resume, unsafe indexing, and new user direction.

## Phase 1 — Fail-closed indexing and storage boundaries

### Work

- [x] Use an explicit text allowlist and sensitive path/name/extension denylist.
- [x] Reject symlinks, traversal, oversized files, raw evidence, and external paths.
- [x] Detect secret-like content before indexing.
- [x] Support file-level `no-rag` markers.
- [x] Require explicit registration for generated safe artifact summaries.
- [x] Enforce per-project indexing policy flags.
- [x] Produce an index preview containing included/excluded reasons and content hashes.
- [x] Remove stale FTS and vector documents when files are deleted or excluded.
- [x] Record index generation and document-set hashes.

### Acceptance

- A nested secret in record metadata cannot appear in continuity views or indexes.
- A symlink to an external or sensitive file is excluded.
- Raw Burp requests, HAR/HTTP files, credentials, and private keys are excluded.
- Preview and apply select exactly the same document set.

## Phase 2 — Canonical continuity journal

### Work

- [x] Store canonical records in `memory/continuity.jsonl`.
- [x] Use a common envelope: identity, timestamp, kind, summary/details, sources, confidence, sensitivity, index policy, supersession.
- [x] Preserve an unfamiliar/original kind label while normalizing a safe kind for routing.
- [x] Sanitize source references with an allowlisted schema.
- [x] Require sources for high-confidence findings/discoveries or downgrade them explicitly; ordinary facts, observations, and decisions remain ergonomic captures without mandatory evidence fields.
- [x] Redact secret values at the write boundary and mark the record `no_rag`.
- [x] Deduplicate recent automatic captures under a file lock.
- [x] Resolve corrections/supersession without deleting history.
- [x] Retain typed JSONL readers/writers only as compatibility adapters.

### Acceptance

- Concurrent duplicate automatic reflections produce one record.
- Corrections replace superseded records in active views while history remains append-only.
- Unsupported kinds survive round-trip with their original label.

## Phase 3 — Deterministic generated views and bounded resume

### Work

- [x] Generate `SITUATION.md` from canonical state.
- [x] Generate `HANDOFF.md` from canonical state.
- [x] Omit empty or irrelevant sections.
- [x] Include recent changes, established knowledge, decisions/corrections, uncertainty, important sources, workspace materials, repository state, and Burp summaries; exclude explicit sensitive records.
- [x] Clearly label continuation points as suggestions.
- [x] Include index freshness and what must not be assumed.
- [x] Return a bounded continuity pack from `project_open`/resume.
- [x] Advance the “changes since handoff” baseline only after producing a resume pack.

### Acceptance

- Rebuilding unchanged state produces byte-identical files.
- Resume is useful without opening raw files.
- A project with no goal or pending item still resumes coherently.
- Explicit user direction is represented as authoritative over suggestions.

## Phase 4 — Preferred six-tool interface

### Work

- [x] Implement and expose `project_open`.
- [x] Implement and expose `project_capture`.
- [x] Implement and expose `project_search`.
- [x] Implement and expose `project_refresh`.
- [x] Implement and expose `project_pause`.
- [x] Implement and expose `project_status`.
- [x] Keep specialized tools as adapters or mark them compatibility-only.
- [x] Route natural-language behavior through `project-continuity/SKILL.md`.
- [x] Reject writes without an explicit or session-attached project.

### Acceptance

- One natural-language storage pattern exists for each common action.
- Preferred operations return actionable content and diagnostics, not only paths.

## Phase 5 — Session identity, switching, and recovery

### Work

- [x] Store attachment under `.harness/state/sessions/<session-key>.json`.
- [x] Make switching atomic and checkpoint observable dirty activity before detaching.
- [x] Prevent concurrent OpenCode sessions from overwriting one another.
- [x] Update session `last_capture_id` after captures.
- [x] Support pause/detach and stale-session preview/recovery.
- [x] Keep legacy state only as a non-authoritative compatibility pointer.
- [x] Add file locks around mutable session and project metadata.

### Acceptance

- Two sessions can attach to different projects safely.
- A direct API project switch cannot discard dirty activity.
- An unclean exit can be diagnosed and recovered.

## Phase 6 — Meaningful automatic continuity capture

### Work

- [x] Capture explicit “remember/save/pause/correct” intents through the skill.
- [x] Capture meaningful Awoki-owned artifact/project operations.
- [x] Add a local OpenCode plugin for tool, file, idle, compaction, delete, and switch events supported by the installed API.
- [x] Pass only sanitized metadata: session ID, tool/event name, relative safe paths, counters.
- [x] Never forward prompts, tool arguments/results, file contents, or reasoning traces.
- [x] Debounce by activity window and meaningful-change thresholds.
- [x] Fail open when event integration is unavailable and report the limitation.

### Acceptance

- Several meaningful operations create one concise operational reflection.
- Trivial activity creates no record.
- Compaction/pause/switch captures bounded observable state only.

## Phase 7 — Exact and semantic retrieval lifecycle

### Work

- [x] Keep the exact project FTS index current immediately after canonical captures.
- [x] Incrementally select documents using hashes and generation manifests.
- [x] Rebuild or replace scoped vector points only for the current document set.
- [x] Delete stale FTS/vector entries.
- [x] Search project continuity first and label global reusable knowledge separately.
- [x] Use generated views first, recent reflections second, targeted retrieval third, raw artifacts only on demand.
- [x] Add explicit repository-only `/codebase` retrieval with exact, FTS,
      Qdrant, and optional reranking while keeping memory reconciliation
      restricted to continuity records.
- [x] Expose freshness and backend status through `project_status`.

### Acceptance

- A newly captured safe record is searchable without a manual index command.
- A deleted or newly excluded source disappears from retrieval.
- Stale semantic indexes are not silently queried.

## Phase 8 — Integrations as adapters

### Work

- [x] Route Burp observations, summaries, and pauses into canonical continuity.
- [x] Register only sanitized Burp summaries as indexable artifacts.
- [x] Remove the built-in credential subsystem; explicit sensitive values use generic secret/no-RAG memory and remain outside generated views and indexes.
- [x] Represent pending work as optional continuation records with lifecycle metadata.
- [x] Preserve repositories, reports, and arbitrary workspace files without forcing a task schema.
- [x] Surface adapter continuity failures as warnings rather than swallowing them.

### Acceptance

- Burp work resumes through the same project handoff as other work.
- A project without Burp or pending state remains fully functional.

## Phase 9 — Migration, compatibility, and diagnostics

### Work

- [x] Implement `awoki migrate PROJECT --preview`.
- [x] Implement `awoki migrate PROJECT --apply`.
- [x] Preserve original records, IDs, and files.
- [x] Identify duplicates and classify sensitive/non-indexable material.
- [x] Rebuild generated views and preview the resulting safe index set.
- [x] Make migration idempotent.
- [x] Implement `awoki doctor` for parse errors, view/index drift, unsafe candidates, and stale/orphan sessions.
- [x] Implement stale-session recovery preview/apply.

### Acceptance

- Re-running migration adds no duplicate canonical records.
- Legacy files are never deleted.
- Diagnostics explain how to repair every reported state.

## Phase 10 — End-to-end validation and release

### Required scenarios

- [x] Create a project from natural language.
- [x] Work without a declared goal or pending item.
- [x] Capture a meaningful observation and search it immediately.
- [x] Enable and search a safe project repository through `/codebase`.
- [x] Correct an earlier assumption.
- [x] Switch projects after meaningful activity.
- [x] Resume from another session.
- [x] Recover from an unclean exit.
- [x] Index a safe report.
- [x] Reject raw evidence, secrets, symlinks, and traversal.
- [x] Remove a deleted indexed file.
- [x] Continue Burp work later through generic continuity.
- [x] Follow new user direction rather than an old suggestion.
- [x] Preview and apply legacy migration twice safely.
- [x] Validate deterministic generated views.

### Release commands

- [x] `python -m compileall -q .harness`
- [x] `python -m unittest discover -s .harness/tests -v`
- [x] `make validate`
- [x] `python .harness/awoki.py doctor`
- [x] `git diff --check`
- [x] Verify Git worktree is clean after commits.
- [x] Produce a portable Git bundle and verify it.

## Release gate

The redesign is complete only when all statements below are true:

- [x] “Resume PROJECT” returns enough real context to continue.
- [x] Awoki records meaningful progress without task syntax.
- [x] `SITUATION.md` and `HANDOFF.md` update without manual editing.
- [x] Arbitrary workspace material can exist without unsafe indexing.
- [x] Project writes cannot leak into global or legacy scope.
- [x] New user direction cleanly overrides continuation suggestions.
- [x] Burp and other integrations use the same continuity engine.
- [x] The model has one obvious preferred operation for each continuity action.

## Completion evidence

Completed on 2026-07-21 against the governing continuity-first design and
updated through 2026-08-04 for remote TEI/Jina retrieval, repository-only
`/codebase`, memory-only reconciliation filtering, internal Qdrant readiness,
macOS path canonicalization, verified runtime migration, and executable `/tmp`
for OpenTUI, strict-model prompt compatibility, tmux runtime integration, and
explicit MCP/retrieval runtime routing.

- 166 unit and integration tests pass under standard sequential discovery.
- The same 166 tests pass across four isolated validation shards.
- `make validate` is the dependency-tolerant host/hermetic gate: JSON/JSONC, manifest/layout contracts, Python compilation, unit/integration tests, shell syntax, OpenCode plugin validation, and locally available parser/Docker checks. `make validate-runtime` additionally requires real ripgrep, the prebuilt Go semantics helper or fixed-source local Go fallback, and Tree-sitter, then executes the runtime code-search/Qdrant gate.
- A clean-root manual lifecycle passes project creation, free-form capture, bounded resume, migration preview/apply/idempotent re-apply, read-only doctor, session preview, and pause.
- Migration retains legacy files and IDs and does not duplicate canonical records.
- `awoki doctor` reports generated-view drift, stale indexes, parse failures, unsafe candidates, and stale/orphan sessions without repairing state.
- OpenCode hook names and compaction integration were checked against the official plugin contract; the plugin passes static TypeScript compilation.
- OpenCode, Bun, and Docker are not installed in the validation environment, so live plugin loading and Docker runtime execution are explicitly environment-limited. Compose validation is skipped with a visible message when Docker is unavailable.
- Final Git commit identifiers and verified bundle location are recorded in the delivery report.
