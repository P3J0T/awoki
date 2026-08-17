# Awoki Agent Rules

Maintainer/context orientation: read `docs/AWOKI_IDENTITY.md` before proposing architectural expansion. v0.1.7 is in stabilization/usefulness-evaluation mode: prefer realistic-work evidence, simplification, merging, and deletion over adding another persistent mechanism. Do not inject the dense identity file wholesale into every normal task.

You are operating inside **Awoki**, a continuity-first project/global memory, retrieval, evidence, and workflow harness for OpenCode.

## Bootstrap

Before project-specific work:

1. Call `harness_status`.
2. If the harness structure is unclear, call `load_manifest`.
3. Read `HARNESS.md`, `.harness/HARNESS.md`, and `docs/RELIABILITY.md` as applicable.
4. Use `project_open` or `recall_context` before relying on earlier project knowledge.
5. Use `search_skills` and `load_skill` before following a specialized procedure.

## Always-on reliability invariants

Treat your own output, memory, and prior model output as fallible, regardless of model size or quantization.

1. Verify concrete claims about source code, configuration, frameworks, runtime behavior, tests, and tool state against actual evidence before asserting them.
2. Never claim a command, test, integration, build, or runtime path was exercised unless its result was observed.
3. Separate observation, inference, and hypothesis. State uncertainty when evidence is incomplete.
4. Preserve source references, corrections, contradictions, scope, and confidence.
5. Prefer the smallest reversible check that can answer the question.
6. Exploration may remain incomplete. Completion claims require evidence proportional to the claim.
7. Never silently broaden scope, promote project knowledge globally, or perform a network delivery action.

These rules guide behavior; deterministic storage, indexing, path, session, and reliability-result boundaries are enforced by Awoki code where possible. They do not guarantee semantic correctness.

Natural wording such as **“verify your findings before answering”** means: use the
applicable exact evidence/runtime checks and do not present a required claim as
fact while it is unsupported, stale, refuted, or contradictory. The user should
not need to know internal claim-state names.

For substantial managed-project conclusions, prefer **bounded self-verification** over recursive self-reflection. Use the `reliability-verification` skill when the task warrants it: preserve expressive claims/hypotheses/observations and non-gating notes as concise assessment nodes, bind them to `ev_...` evidence where available, connect them with first-class relations, and run a deterministic `reliability_verification_checkpoint`. If one corrective action is justified, consume the run-owned corrective budget before doing it and then perform one final checkpoint. Zero required machine claims are `NOT_APPLICABLE`, not vacuous success, and composed reliability/acceptance work must use the explicit cross-ledger aggregator. Do not store private reasoning, do not treat model inference as machine proof, and do not let an unrelated degraded backend block a claim unless that capability was explicitly required.


OpenCode agent-runtime anomalies are separate from epistemic correction. Awoki may persist structural terminal-turn metadata (finish reason, reasoning/text/tool presence, completed tool count, provider/model/mode, and bounded token counters) through `session_runtime_status`, but never reasoning content. `reasoning_only_terminal_turn` and `tool_execution_without_followup` are runtime degradation, not evidence failure, and manual follow-up recovery attempts do not consume a reliability corrective budget. Awoki does not automatically continue generic failed model turns.

## Continuity

The user drives project direction. Do not force goals, task lists, pending items, or a single current objective.
For a genuinely multi-step natural-language investigation that is likely to span several tool calls or a context compaction, use OpenCode's native TODO list early as a **small active working set**: summarize the user's requested deliverables/constraints in roughly 3–8 bounded items, not the raw prompt and not every intermediate thought. Keep it aligned when the user changes direction. Awoki already mirrors this TODO projection outside conversational context, so this is the preferred goal-continuity mechanism; do not invent a separate session-intent ledger merely to restate the prompt.

Preferred operations:

- create/open/resume/switch: `project_open`
- add/list/remove/default project repositories: `project_repo_add` / `project_repo_list` / `project_repo_remove` / `project_repo_default`; plain “add repo oathkeeper” should infer `repo/oathkeeper`
- save/remember/note project knowledge: `project_capture`; use neutral `observation` by default and stronger kinds only when the user explicitly states or clearly implies them
- search project continuity and indexed material: `project_search`
- refresh generated views and indexes: `project_refresh`
- pause/handoff the whole project/session: `project_pause`
- checkpoint/status/finalize one long-running generic task: `project_task_checkpoint` / `project_task_status` / `project_task_finalize`; never use `burp_task_*` unless the work is actually a live Burp workflow
- diagnose state and freshness: `project_status`


Capture taxonomy is metadata, not ceremony. A plain save must not be turned into a finding merely because it is useful. Facts and observations do not require an evidence form to be stored. Findings/discoveries are evidence-oriented, especially when marked high confidence. Durable `cont_...` IDs remain internal references for correction, supersession, deduplication, and diagnostics; ordinary users need not manage them.

These names are MCP tools, not shell commands. Call them through the Awoki MCP.
Never invent Bash commands such as `awoki_project_refresh` or
`awoki_project_open`. A shell `export` affects only that shell and cannot
reconfigure an MCP process that OpenCode already started. For `.env` changes,
recreate the OpenCode service and start a new OpenCode process before testing.

## Deterministic repository-analysis default

For an attached Awoki project, code/evidence analysis is evidence-backed by default.
Projects may use either the legacy exact Git root at `repo/`, an explicit
multi-repository registry under `repo/<repo-id>/`, or registered non-Git textual
evidence corpora under `sources/<source-id>/`. Git remains commit/view-bound;
non-Git corpora use a canonical content-manifest revision identity. Broad
`codebase_search` spans enabled registered evidence sources; exact source, graph,
evidence, and related operations use `repo=` for Git compatibility or `source_id=`
when scope would otherwise be ambiguous. Every result carries source/revision identity.
The user does not need to say "deterministically" or invoke `/codebase` explicitly.
Natural-language requests such as "explain", "trace", "understand", "find how",
"show the flow", or "how is this input processed" must use the indexed repository
workflow automatically.

When `project_open` or `project_repo_add` returns `repository_index_advice`, surface
it immediately. Distinguish structural/FTS readiness from semantic-vector
readiness. A missing/stale existing local structural snapshot should be refreshed
through detached `code_index_refresh_start` first; report the job id and return
control, use `code_index_refresh_status` only on a later user/status-dependent turn,
and cancel only with `code_index_refresh_cancel` on explicit request. This job is
local-only and performs no remote embedding/Qdrant writes. Missing/stale vectors
after structural refresh should produce a concise offer to run the returned explicit
`code_vector_refresh_start` action. This also starts a detached worker and must return quickly; report the job id and return control to the user. Never autonomously poll a long-running index/vector job with the model. Call `code_vector_refresh_status` when the user asks for status or when a later requested action genuinely depends on fresh state, and use `code_vector_refresh_cancel` only when the user asks to stop it. For explicit repository-readiness workflows, `repository_prepare_start` is the durable source of truth: one parent job owns structural refresh, vector materialization, exact membership verification, and backend probes without model polling. Optional continuity may observe only bounded parent-job metadata and best-effort resume a conversation after a terminal transition; conversation wake-up is not a readiness correctness boundary. Status exposes bounded parent/child progress without source text. Opening a project or registering a checkout/corpus is never permission to perform remote embedding work silently. Non-Git vector materialization requires an explicit `source_id=` refresh. First-time vector materialization may be CPU-intensive at the configured embedding backend.
For explicit requests to prepare/prime/warm a managed repository for code review or full retrieval, use the `repository-readiness` skill and `repository_prepare_*`. Treat explicit full semantic/hybrid readiness as vector-materialization intent for that exact managed scope; ambiguous "index" requests remain local-ready until remote embedding intent is clarified. OpenCode TODO is only a visible projection; the project/repo/source-scoped parent preparation job is durable truth. Existing explicitly named projects can be prepared while unattached; never auto-switch away from another attached project, and never silently create a managed project for true ad-hoc work. Bounded transient embedding retries/adaptive request-size reduction belong inside the vector worker; failed whole jobs are not restarted indefinitely and incomplete vector membership never counts as FULL_READY.

OpenCode TODO state is mirrored into a bounded session-local work ledger so operational progress and the small active working set survive compaction even for unattached/ad-hoc sessions. The ledger is not canonical project knowledge and must never override the newest user instruction. A new user turn marks the prior TODO/reference snapshot as needing review until the session's working set is reconciled. Use `session_work_status` when exact post-compaction operational state is needed. Do not automatically recreate/overwrite native OpenCode TODOs from the mirror; reconcile deliberately with the newest user request. Human references are injected after compaction only when they were actually used in the current session (plus references required by an active acceptance run); old project references remain searchable but are not made "active" merely because they were saved recently.

For multi-step acceptance/benchmark runs whose final report depends on exact earlier tool observations, start a durable `acceptance_run_*` ledger before the first test with expected test/invariant IDs when known. R9.1.6.15 acceptance v4 persists bounded machine-checkable test contracts: required execution MCP interfaces, required acceptance-orchestration interfaces, required scalar observations/pass conditions, current-run evidence requirements, native-tool allowlists/count limits, optional per-interface invocation ceilings, forbidden tool classes, and stop boundaries. Each test keeps bounded immutable `aat_` attempt history so an earlier machine downgrade is never overwritten by a later bookkeeping correction; acceptance re-recording does not consume the separate reliability corrective budget. `acceptance_run_record` returns the new attempt ID/effective outcome plus bounded immediately-prior attempt context; correction drills may additionally declare `prior_attempt_requirements`, which Awoki evaluates directly against immutable attempt history. Use those machine-owned facts or `reference_describe(aat_...)` instead of writing self-referential requirements about an attempt's future machine outcome. Call `acceptance_run_next` after every record and again after compaction instead of re-planning the suite scheduler in model reasoning; it returns the exact current durable contract plus separate execution/orchestration provenance. The OpenCode plugin records only tool name/class/start/completion metadata for the active test—never arguments, output, source, or private reasoning. `acceptance_run_record` may downgrade a claimed PASS to `incomplete` or `protocol_deviation` when the durable contract was not actually satisfied. When detailed retrieval support may be needed later, use Awoki retrieval with `capture_evidence=true` plus the exact acceptance run ID, then record the returned `evidence_ref` and canonical candidate IDs rather than copying raw source/tool output into the ledger. Stable `ev_` identity remains content-addressed; a bounded non-RAG capture-provenance sidecar lets a contract require evidence captured in this acceptance run. Keep `evidence=` to small scalar observations, record cross-test invariants explicitly, and aggregate the final report from `acceptance_run_status` rather than conversational memory. `harness_self_check` is the only MCP path for allow-listed Awoki self-regression groups; it is not a generic command runner. Use `acceptance_evidence_get` only when richer prior support is actually needed after compaction/restart. The ledger is bound to the managed project/source/revision and published vector membership captured at start; source/revision/membership drift blocks further recording/finalization. Incomplete finalize is rejected and remains resumable. Content-addressed rich evidence lives under project `artifacts/acceptance/raw/`, is explicit-retrieval-only and never registered for RAG. Persistence does not upgrade model-recorded evidence into machine proof. Acceptance notes are intentionally compact; when a durable object matters to humans, use `reference_describe`/`reference_annotate`/`reference_resolve` so the stable ID remains authoritative while `label` and `why_saved` explain what it is and why it was retained. Natural-language resolution is navigation only and never replaces exact evidence/provenance identity; close/low-confidence matches return an explicit ambiguous result with no resolved stable ID. Candidate references distinguish `first_materialized_in` from later `observed_in` evidence occurrences. Compaction events retain a bounded trigger identity (`automatic_context_pressure`, `explicit_request`, or `unknown`) rather than asking reports to infer automatic vs explicit compaction from generation numbers.

Use this order:

1. Establish the evidence-source boundary from the attached project's code
   status/search scope. `CONTENT_MANIFEST_BOUND` is the non-Git corpus equivalent:
   exact source bytes are bound to a canonical whole-corpus manifest identity. `VERIFIED_SNAPSHOT` means the exact Git worktree root,
   commit/view, and a clean materialized source snapshot were deeply checked at
   index/verify time; `WORKING_TREE_BOUND` means source is still usable but the
   whole worktree is not claimed as one immutable snapshot; `FILESYSTEM_BOUND`
   means Git provenance is unavailable. Reduced assurance is a warning, never a
   reason to hide otherwise-readable source.
2. `codebase_search` to discover candidate files and symbols. Semantic/FTS/Qdrant
   hits are discovery only and are never behavioral proof. A normal interactive
   search must reuse a current structural snapshot and must not implicitly rebuild
   remote code vectors; stale/unavailable semantic backends degrade to local
   structural/FTS discovery. Refresh vectors explicitly when required.
   Conceptual retrieval keeps tests/config/schema/docs in the discovery universe,
   but for implementation/runtime/security intent it prefers query-relevant
   **concrete** production implementations plus deterministic diversity. Strong
   coarse production module/file hits may be structurally refined into contained
   methods/functions and independently reranked against the original query; parent
   relevance grants evaluation capacity, not authority. A structurally connected
   production symbol is likewise only a promoted candidate: verified graph
   connectivity is not behavioral relevance, and the candidate must still earn
   relevance against the original query before it can rise. When the user
   explicitly asks for tests/config, honor that focus instead of forcing
   production-first ranking.
3. Resolve exact symbols with `code_definition`, `code_callers`, `code_callees`,
   and `code_path` where applicable.
4. For flow-oriented questions, use `code_flow_graph` from the exact entry point to
   build a bounded relevant reachable graph. Traverse only resolved edges; keep
   ambiguous/unresolved edges as explicit boundaries.
5. Use `code_source_window` to inspect bounded, hash-checked current source for
   conditions, assignments, aliases, arguments, returns, and outcomes. Do not
   require complete arbitrary-length source lines. Its `evidence_id` binds the
   returned range to exact source bytes plus Git repository/view identity (including
   commit/raw-tree and tracked HEAD blob identity when available) or non-Git
   source/content-manifest identity. Re-run
   `code_evidence_verify` after edits, branch/view changes, or a long-running
   investigation before reusing old source evidence. The evidence ID is a
   compact checksum-protected token for stale detection, not a signature or origin
   attestation.
6. Use `code_validate_claim` selectively for important bounded propositions that
   its strict proof profile supports. Do not force an entire architectural or
   execution-flow question into one atomic claim.
7. If `code_source_window` advertises `deterministic_semantics.recommended=true`,
   run the listed operation(s) before making that concrete primitive claim. If a
   concrete claim depends on a supported Go language/stdlib primitive
   (`path.Join`, `path.Clean`, `time.ParseDuration`/duration multiplication,
   failed `error` type assertion, `strings.Replace`, `url.Parse`, or the
   allow-listed `httputil.ReverseProxy` forwarded-header probe), call
   `code_semantics_check` and use its observed result instead of mental
   arithmetic or remembered semantics. Docker executes a fixed allow-listed
   stdlib helper precompiled by the pinned Go builder stage; source-tree
   development may compile the same fixed helper locally. Repository code is
   never compiled/executed and the helper has no network path. Respect
   `toolchain_alignment`: a helper stdlib observation is not target-runtime proof
   when the project's declared Go major/minor differs.
8. Present confirmed behavior only to the level supported by the current source
   and structural evidence. Mark dynamic, ambiguous, unsupported, or stale
   boundaries explicitly.

For a broad execution-flow question, construct the relevant subgraph rather than
dumping the entire repository graph. A call graph alone is insufficient when
behavior is encoded in conditions or local data flow; inspect exact source for
branch predicates, assignments/aliases, argument passing, and terminal outcomes.
A static graph is possible flow, not proof that a runtime execution occurred.

Tests and fixtures are first-class evidence but are not production implementation by default.
For ordinary behavior questions, prefer production source in discovery/ranking and use tests to
corroborate edge cases, intended contracts, regressions, and failure modes. If the user explicitly
asks about tests, edge cases, fixtures, or regressions, promote test/test-fixture evidence rather
than suppressing it. Never exclude tests merely to make a production explanation cleaner.
For security-boundary questions (authentication locations, forwarded headers, path normalization,
large/empty bodies, error selection, rate limits, malformed inputs), deliberately inspect relevant
test evidence after locating the production path; these tests often encode the boundary conditions
that a generic production-source search will not rank first. Treat the test as evidence of an
expected/regression contract and verify the production implementation before asserting current
runtime behavior.

Search diagnostics are machine-readable. When evaluating retrieval quality, use
real `mode=lexical` or explicit `use_fts` / `use_qdrant` / `use_reranker` controls
rather than asking the model to simulate a backend. Use `strict_backends=true`
when an explicitly requested semantic backend must succeed rather than degrade.
Use `view=diagnostics` for large retrieval acceptance/debug runs. It returns
telemetry before hit data, omits source previews, and stores the complete bounded
candidate pool as a project-scoped metadata-only trace. Pass `diagnostic_targets`
for exact candidates that must be visible in the first response; use
`code_diagnostics_trace` for bounded paging or path/symbol lookup of the full
trace. Never scrape OpenCode's cached tool-output files to reconstruct a response.
The trace handle is observability state only and must not be treated as source
evidence or as permission to alter retrieval state.
Never infer that reranking ran from score shapes: use the returned
requested/attempted/applied/backend telemetry and per-hit stage scores/ranks.
For normal conceptual code questions, optimize for useful results: embeddings and
FTS broaden discovery, concrete-symbol refinement turns strong coarse production
hits into independently evaluated callables, and focus-aware reranker selection
reserves bounded evaluation capacity for strong intent-matching/refined candidates
without giving them score or rank for free. An already-discovered concrete child
requalified by refinement is still eligible for that bounded evaluation window;
deduplication must not erase the refinement relationship. Final ordering is authority-aware
and relevance-gated. Reranker rank is combined with retrieval rank rather than
treated as a directly comparable raw score.
Do not globally exclude tests, and do not promote weak production code by role
alone. Structural promotion creates candidates only; production representation in
the top results requires independent retrieval/query support.
Unknown explicit retrieval modes are errors, not invitations to silently choose
another mode.

Choose exact-search tools by intent rather than enforcing a native-tool ban in normal repository work:

- conceptual/architectural discovery -> Awoki `codebase_search` first;
- known string/symbol lookup -> OpenCode `Grep` is the normal bounded exact-search tool;
- complex or exhaustive exact enumeration where ripgrep flags/counts/context/globs materially help -> `code_exact_search` is the first-class structured ripgrep path;
- machine-checked exhaustive repository coverage or recovery from client/giant-line transport limits -> Awoki `code_text_search`.

`code_exact_search` is a first-class exact-search tool and does not need an Awoki semantic search to "fail" first when the question itself is exact enumeration. For broad conceptual investigations, still start with indexed/structural discovery rather than spraying repository-wide regex. If OpenCode Grep or `code_exact_search` truncates/errors or cannot establish the coverage needed for a claim, switch to `code_text_search`, follow its cursors to completion, and require `repository_universe_complete=true` before claiming exhaustive repository-source coverage. Use `include_ignored=true` only for explicit forensic scope that must include Git-ignored untracked files. Do not pipe an exhaustive fallback through `head` or another truncation layer. All lexical search output remains `DISCOVERY ONLY`: reopen authoritative source before asserting behavior. Acceptance/benchmark contracts may deliberately forbid native tools; those machine-enforced contracts override this normal-work ergonomics policy.
If Awoki MCP itself is unavailable, `.harness/bin/code-search-fallback` is the diagnostic equivalent for exhaustive local lexical coverage; it is not the preferred normal interface.

Before finalizing a repository-behavior answer, run a consistency pass over
concrete claims. If a result depends on language/runtime library semantics,
duration/unit arithmetic, path joining/normalization, or another deterministic
primitive, use `code_semantics_check` when it supports the operation; otherwise
verify from the target toolchain/source or another exact local check rather than
intuition. Reconcile repeated claims and tables against
one another; do not leave mutually contradictory concrete values in the same
answer. Edge-case statements need evidence just like main-path statements and
must be labeled as inference when the repository does not establish them.

Git provenance is evidence about repository objects and source snapshots, not
proof of human identity. Author/committer names and emails are metadata claims.
Replacement refs, sparse checkout, submodules, configured worktree filters, and
other unusual Git view state reduce or qualify repository assurance but must not
silently remove readable code. Passive Awoki Git inspection disables fsmonitor
and configured content-filter execution; it may conservatively report a filtered
worktree as dirty rather than execute repository/local helper commands. Awoki
cannot prove that remote/unreachable history never existed when no local or
external anchor remains.

Use `cross_project_code_search` only with an explicit project list or explicit
`all_indexed=true`; never silently widen repository scope.

The `/code-validate-claim` command remains a natural-language verification front
door. Broad verification requests must first be discovered and decomposed into
exact obligations before the strict `code_validate_claim` MCP primitive is called;
do not pass a vague request directly to that primitive. The primitive reparses
current hashed source and returns `VERIFIED`, `REFUTED`, or an explicit uncertainty
verdict without embeddings or reranking.

Project-local knowledge overrides global knowledge. The user's new instruction overrides generated continuation suggestions.

## Command interface

Natural language is the default interface. Slash commands are stable intent anchors, not one wrapper per MCP tool. Use the authoritative surface in `docs/COMMANDS.md`.

- `/project` routes project create/open/switch/refresh/capture/search/pause requests.
- `/codebase` routes normal repository questions; infer `peek`, `context`, or `full` view from wording rather than requiring separate view commands.
- `/burp` routes read-only live inspection, archive lookup, summarization, and safe preservation.
- Keep `/definition`, `/callers`, `/callees`, `/code-path`, `/code-across`, and `/code-validate-claim` separate because they have distinct deterministic semantics.
- Keep `/burp-send`, `/burp-repeater`, and `/burp-intruder` separate because they express explicit side-effect intent.

Do not invent removed aliases or expose every internal Python/MCP operation as a slash command. Underlying tools remain available to skills and maintainers.

## Memory and sensitive values

- Normal project/analysis memory is coverage-first: security vocabulary and code snippets remain retrievable. High-confidence credential values are redacted best-effort, but redaction alone does **not** mark the surrounding record `no_rag`.
- When the user explicitly asks to save sensitive plaintext, use the explicit sensitive-memory option. Preserve the value as requested, mark it secret/no-RAG, and do not put it in generated views or automatic retrieval.
- Retrieve sensitive records only when the user explicitly asks for them.
- Do not invent a credential backend or claim encryption that is not present.
- Never send no-RAG records to embedding or reranking endpoints.

## Retrieval

Qdrant is the semantic retrieval store. Embeddings are produced by the configured remote OpenAI-compatible endpoint. SQLite FTS remains available for lexical retrieval and continuity resilience.

- Index only material allowed by project policy.
- `/codebase` explicitly enables the active project's dedicated structural code
  index. Eligible source is parsed into symbol-aware chunks where a curated
  Tree-sitter grammar is available; curated textual source/interface/policy
  formats use deterministic text-fallback chunks when no grammar exists. Other
  textual repository formats remain available to exhaustive lexical search.
  Definitions, exact occurrences, FTS, branch-scoped Qdrant vectors,
  conservative call edges, and optional reranking remain separate evidence
  sources inside one native Awoki engine.
- `retrieval_status` and `code_index_status` are passive status reads: they do
  not perform network probes or repository-wide source scans. `retrieval_status`
  reports shared embedding/reranker configuration plus last-known Qdrant health;
  `code_index_status` reports materialized parser/branch/SQLite/graph/vector state
  and can prove source freshness cheaply for a clean Git worktree at the indexed
  commit. Use explicit `retrieval_probe` for live backend checks and
  `code_index_verify` for byte-level source freshness plus optional live
  code-Qdrant verification.
- `project_search` may search the broader safe project document set. Memory
  reconciliation during `project_capture` must compare against continuity
  records only, never repository chunks.
- Raw Burp traffic, explicit no-RAG material, private keys, and environment files must not enter FTS, Qdrant, embedding, or reranking requests. Coverage-first local lexical diagnostics may still account for textual secret/config files with opaque previews; security vocabulary, endpoint names, and auth-related paths are never themselves exclusion reasons.
- Before semantic indexing or diagnosing retrieval, call passive `retrieval_status` and compare its effective endpoint, model label, vector size, collection, and reranker state with the intended runtime configuration. Use `retrieval_probe` only when live connectivity must be tested.
- If the effective runtime profile differs from `.env`, do not attempt to repair it with a transient Bash export; report the mismatch and recreate/restart the owning process.
- If remote embedding or reranking is unavailable, report the degraded state; do not fabricate semantic results.

## Awoki self-development boundary

Before modifying Awoki itself, run `.harness/bin/awoki-dev-preflight` (or `make dev-preflight`) and require it to pass. It verifies that the current working root is the
writable top-level Awoki Git checkout: `.git` must exist at that root, `git
rev-parse --show-toplevel` must resolve to it, and the intended files must be
writable without privilege escalation. The hardened `awoki-opencode-ssh`
runtime at `/awoki` may intentionally contain runtime source without the
top-level Awoki `.git` and may be read-only to `op`; it is not a development
checkout. If this check fails, stop and report the environment mismatch. Do not
try `sudo`, `su`, `chown`, `chmod`, ownership changes, `/root` discovery, or
other privilege-escalation/workaround behavior to turn the runtime appliance
into a development checkout. Use a separate writable host/dev clone.

## Backup and restore

Use `.harness/bin/awoki-backup` or the Make targets documented in `docs/BACKUP_RESTORE.md`. Prefer a portable backup for migration. Never include `.env`, SSH client material, or OpenCode state unless the user explicitly requests the relevant sensitive option. Require all services stopped for full backup and every restore; only portable backup may use explicit live-capture acknowledgement. Raw Qdrant backup/restore is never allowed while Qdrant runs. Verify the `.sha256` sidecar before restore while remembering it is integrity—not origin authentication—refuse overwrite without explicit force, and do not claim a full Qdrant backup is compatible across changed Qdrant images, vector dimensions, collections, embedding providers/request labels, normalisation, or the actual model/revision declared by `AWOKI_EMBEDDING_DEPLOYMENT_ID`.

## Burp

Burp is an optional adapter, not a property of every Awoki project. Do not create
`artifacts/burp/`, load Burp archive state, or route generic project work through Burp tools
unless the user is actually doing Burp/web-security work or explicitly asks to preserve Burp
evidence. Project Burp storage is created lazily on the first Burp write. Generic project/global
recall does not mix Burp inventories into unrelated work.

The `burp-workflow` skill is authoritative. Use `/burp` or ordinary natural language for live inspection, searching, summarizing, archive lookup, and safe preservation.

- Use direct Burp MCP for live state and actions; use Awoki for compact project evidence, continuity, and sanitized summaries.
- Network sends, active-editor mutation, Repeater creation, and staging into Intruder require explicit user intent in the current request.
- `/burp-intruder` stages only; it does not authorize an attack. `/burp-send` authorizes one unambiguous send, not retries, scanning, or escalation.
- Do not substitute assumptions or unrelated HTTP tools for live Burp state.
- Keep raw Burp traffic, cookies, tokens, and credentials out of broad memory and RAG.

## Operating modes

- `/explore`: free investigation with the always-on invariants intact.
- `/verify`: focused evidence review of important claims.
- `/reliability-check`: local, adaptive proof of the completion or pause claim; no push or PR.
- `/ship-check`: explicit delivery workflow; remote actions require explicit user authorization. Ship mode must pass the structured claim gate: required claims need deterministic verifier receipts, stale/inconclusive claims block, and refuted/contradictory verified claims fail.

Load the `reliability-verification` skill for verification, reliability, or shipping work.


## SSH runtime environment diagnostics

- The SSH shell does not ambiently inherit Compose retrieval/Burp/Lavish settings; Awoki MCP restores the root-owned `/run/awoki/runtime.env` snapshot itself. `make opencode-runtime-check` refreshes that snapshot as root before validating the `op` runtime, so a missing snapshot is repaired rather than treated as a permanent install failure.
- Normal skills use MCP. Never `cat` or manually source the runtime snapshot. For an explicit shell diagnostic use `make runtime-config`, `make embedding-benchmark`, `make reranker-benchmark`, or `.harness/bin/awoki-runtime-env --profile <profile> -- <trusted-command>`. The wrapper must stay compatible with macOS system Bash 3.2 because `make validate` exercises it on the host.
- `retrieval`/internal `mcp`/`all` can expose configured API keys to the child. Never run repository code, Git/build/test commands, or downloaded tools under those profiles. `burp` and `lavish` intentionally exclude retrieval API-key variables from child environments. This is ambient-environment minimization, not a same-user sandbox: the current stdio-MCP design still requires the `op` runtime user to be able to read the tmpfs snapshot, and same-user OpenCode state may also hold provider authentication. Treat target repositories as data; execute hostile target code only in a separate credential-free sandbox.
- Live Burp remains direct `mcp.burp`; the `burp` runtime profile is only for Awoki archive/helper CLI.
- Repository-facing harness subprocesses must not inherit retrieval/provider credentials. Passive Git and `rg` use the credential-free subprocess environment; deterministic semantics helpers use fixed clean environments. Do not add a new repository/toolchain subprocess without an explicit environment contract and regression coverage.
- Critical runtime assumptions are recorded in `.harness/runtime-dependencies.lock.json`; `make dependencies-check` must pass. OpenCode CLI/local-plugin/SDK compatibility, the Lavish helper default, Qdrant default image, builder/runtime images, and direct Python requirements must be updated through that lock and their referenced build/runtime/config files in one reviewed change. Never rely on OpenCode auto-update or an unrecorded manual package/image upgrade for a release.
