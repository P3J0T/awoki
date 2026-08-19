# Machine Harness Notes

This file is always loaded by OpenCode. The complete machine-readable map is `.harness/manifest.json`. R9.1.6.18 is in stabilization/usefulness-evaluation mode; architecture expansion should be justified by observed real-work failures rather than added spec surface.

Use `project_open` for named project work and `project_status` when session attachment or index freshness is unclear. Project memory shadows global memory. The user’s new direction overrides generated continuation suggestions.

## Reality check

Treat model output, memory, and prior summaries as fallible. Concrete code, configuration, runtime, test, and tool-state claims require observed evidence. Never claim a check ran unless its result was observed. Exploration may remain incomplete; completion claims require evidence proportional to the claim. See `docs/RELIABILITY.md`.

Awoki provides:

- append-only continuity and generated bounded resume views;
- SQLite FTS plus Qdrant semantic retrieval;
- remote OpenAI-compatible embeddings;
- optional remote HTTP reranking;
- project/global promotion review and demotion;
- explicit sensitive no-RAG memory when the user requests it;
- direct Burp MCP routing through the existing `burp-workflow` skill.

Awoki does not include a credential backend or local embedding/reranking model weights.

## User command surface

Natural language is primary. `/project`, `/codebase`, and `/burp` are the broad front doors. Precision commands remain only where semantics or side effects differ materially. See `docs/COMMANDS.md`. Do not invent slash aliases for internal MCP tools or Burp archive helpers.

## Retrieval and sensitive data

Only policy-allowed content may reach FTS, embedding, Qdrant, or reranking endpoints. Explicit sensitive records remain no-RAG and outside generated views and automatic retrieval. `retrieval_status` and `code_index_status` are passive/local reads and never perform live network or repository-wide verification. Use `retrieval_probe` for explicit bounded backend checks and `code_index_verify` for explicit deep source/code-vector verification. Never expose API keys.

## Continuity-first invariant

The canonical project store is `workspace/projects/<project_id>/memory/continuity.jsonl`; `SITUATION.md` and `HANDOFF.md` are generated views. Store concise operational reflections, never private chain-of-thought.
Generic project saves use neutral `observation` records unless the user explicitly supplies a stronger semantic kind. Evidence/confidence structure is optional for ordinary facts and observations; high-confidence findings/discoveries remain evidence-oriented. Continuity record IDs are durable internal references used for correction/supersession and diagnostics.

Preferred tools: `project_open`, `project_capture`, `project_search`, `project_refresh`, `project_pause`, and `project_status`. These are MCP calls, not shell commands; never invent `awoki_<tool>` executables. Use `retrieval_status` before semantic indexing when runtime configuration is uncertain. A Bash `export` cannot change the environment of the already-running MCP process.
Git repository membership uses `project_repo_add`, `project_repo_list`, `project_repo_remove`, and `project_repo_default`; natural language such as “add repo oathkeeper” should infer `repo/oathkeeper`. Non-Git textual evidence uses `project_source_add`, `project_source_list`, `project_source_remove`, and `project_source_default` under `sources/<source-id>/`; its deterministic revision is a canonical content-manifest hash. Registration is local-only and never authorizes remote embedding. `project_open` and `project_repo_add` return passive `repository_index_advice`; surface missing/stale vector state and offer the explicit recommended refresh, but never trigger remote embedding merely because a project was opened or registered.

Use `codebase_search` or `/codebase` for managed code/evidence questions. Projects may
use the legacy exact root at `repo/` or registered exact child roots under
`repo/<repo-id>/`; broad discovery spans enabled children and exact operations use
`repo=` when ambiguous. The first code-analysis call explicitly enables the active
project's dedicated structural code index.
Interactive search never performs a remote Qdrant/vector rebuild implicitly. A
first local index may be established on demand, but an existing stale structural
SQLite/graph snapshot and every explicit `refresh_index=true` request are routed to
detached `code_index_refresh_start` so a full repository parse cannot consume the
MCP request deadline. `code_index_refresh_status` reports bounded file/parser/current
path progress without source text; cancellation is explicit through
`code_index_refresh_cancel`. Local index jobs perform no remote embedding/Qdrant
work. Search reuses a clean current Git snapshot without rescanning every source
file and uses Qdrant only when the previously materialized vector membership is
already current. Remote query embedding/Qdrant/reranking failures are bounded and
degrade to local structural/FTS discovery instead of consuming the MCP request
deadline. Refresh vectors explicitly with `code_vector_refresh_start`; it runs detached, returns control immediately, and can be stopped with `code_vector_refresh_cancel`. Non-Git corpora require an explicit `source_id=` vector refresh so registration alone never widens remote-upload scope. Never autonomously poll a long-running index/vector job with the model. Use status tools when the user asks or when a later requested action needs fresh state. For explicit repository readiness, `repository_prepare_*` owns structural refresh, vector materialization, membership verification, and backend probes as one detached project/source-scoped parent workflow; model/session wake-up is optional UX, not a readiness dependency.
Use the `repository-readiness` skill for explicit repository preparation/priming so local structural readiness, vector publication, and retrieval-backend configuration are verified as separate gates. OpenCode TODO is visible progress only; native TODO updates are mirrored into bounded `.harness/state/work-ledger/` state with Awoki-owned stable `atd_` identities so compaction can preserve operational progress even when no project is attached. For multi-step natural-language work this existing TODO projection is the small active working set for user-requested outcomes/constraints; keep it bounded rather than creating another session-intent ledger. The same session ledger tracks only human references actually used in the current session, preventing old project references from being injected by recency alone. New user direction marks older working-set state for review and always wins. An explicitly named existing project may be prepared while unattached; a different attached project blocks optional background resume; true ad-hoc work is never silently promoted to a managed vector scope. Multi-test acceptance work uses compact project-scoped `acceptance_run_*` v4 ledgers plus opt-in content-addressed `ev_` evidence artifacts under `artifacts/acceptance/raw/`: v4 preserves bounded per-test PASS criteria, immutable `aat_` attempt history, separate execution/orchestration provenance, optional per-interface call ceilings, and compaction generation/history with structural trigger identity; a claimed PASS is downgraded when declared protocol/evidence requirements are not satisfied. `acceptance_run_record` also returns a bounded summary of the new immutable attempt plus immediately-prior attempt context; optional `prior_attempt_requirements` are evaluated against immutable machine-owned attempt history, so bookkeeping corrections can use already-computed machine outcomes instead of self-referential future-outcome requirements. Stable evidence identity stays content-addressed while a non-RAG sidecar records capture-run provenance. `harness_self_check` is an allow-listed MCP regression runner, not generic shell execution. `acceptance_evidence_get` can recover exact supporting Awoki evidence on demand. `reference_describe`/`reference_annotate`/`reference_resolve` add non-RAG human labels and `why_saved` navigation while stable IDs remain authoritative; ambiguous natural-language matches return no resolved ID, and `cand_` descriptions distinguish first materialization from later evidence occurrences. Incomplete finalization remains resumable and source/revision/membership drift fails closed.
For bounded self-verification, the `reliability-verification` skill can record flexible assessment nodes (claims, hypotheses, observations, questions, contradictions, gaps, decisions) whose strict parts are authority, provenance, relations, and `ev_` references. `reliability_verification_checkpoint` checks graph/evidence coherence and only treats a degraded backend as blocking when that capability was explicitly required; at most one corrective evidence action is permitted before a final checkpoint.
The vector job protocol also enforces a bounded polling cadence with
`recommended_poll_after_seconds`, `next_poll_after`, `poll_too_soon`, and
`retry_after_seconds`. Partial failures retain already-persisted/reused vector and
batch progress. A full-reuse refresh batches Qdrant inventory and skips unchanged
payload writes instead of issuing one mutation per current point.
Repository understanding is evidence-backed by default: semantic/FTS/Qdrant hits
locate candidates but are not behavioral proof. Resolve exact symbols, use the
internal `code_flow_graph` tool for bounded relevant reachable graphs, and use
`code_source_window` for bounded active-branch hash-checked source before asserting
flow behavior. Each source window carries an `evidence_id` binding its range to
exact source bytes and repository/view identity; use `code_evidence_verify` after
edits or Git-view changes before reusing old evidence. The token is checksum-
protected for stale detection, not a signature or origin attestation. Inspect
branch predicates, assignments/aliases, arguments, returns,
and outcomes rather than treating a call graph alone as complete data/control
flow. Ambiguous or unresolved graph boundaries must remain explicit.

Conceptual retrieval is authority-aware but not test-blind. FTS and current
Qdrant vectors discover candidates; raw stage ranks/scores survive fusion; bounded
verified structural edges may add production candidates from strong test/config
hits; strong coarse production module/file candidates may be refined through exact
structural children plus exact-file symbol enumeration into bounded concrete
functions/methods; the
focus-aware reranker selector allocates bounded capacity across broad discovery,
result-focus candidates, and refined/promoted candidates; the optional reranker
evaluates that selected window against the original query and code search requests
one returned score per selected document when the backend can provide it; retrieval
rank and reranker rank are fused without comparing incompatible
raw scorer scales; then a bounded result-focus authority prior and deterministic
diversity produce final top-K. Structural connectivity/containment is candidate-
generation evidence, never proof that the target answers the query. Without an
independent reranker score, promoted/refined candidates cannot receive a normal
production-authority boost unless conservative local query overlap supports it.
Explicit test/config queries retain those roles instead of forcing production.
Reserved test/config focus capacity is relevance-gated: matching the requested
source role alone is insufficient. Candidates also need independent query/backend
evidence, and unused reserved capacity is refilled only by independently relevant
rows; low-evidence leftovers may leave part of the remote reranker budget unused
rather than being quota-filled. Telemetry reports rejected refill rows and unused
capacity. Named structural declarations are resolved through bounded
declaration-wrapper nodes without descending into parameters/bodies, so real
source names are preserved across grammar shapes instead of becoming anonymous
symbols when the grammar nests the owner below the indexed declaration node.

For retrieval diagnostics, use real `mode=lexical` or per-query `use_fts`,
`use_qdrant`, `use_reranker`, `structural_promotion`, and
`result_focus=auto|implementation|balanced|tests|config`. Unknown modes are
rejected. `strict_backends=true` means requested Qdrant/reranking must actually
succeed. Read the returned requested/attempted/applied reranker telemetry and
per-hit FTS/Qdrant/fused/rerank/final ranks/scores; never infer backend execution
from score shapes. Reranker telemetry distinguishes pool/budget and selection lane,
selected/requested documents, configured versus effective requested top-N, explicit
scores returned, selected candidates without returned scores, and the complete
post-rerank pool. Result preview budgets never silently discard requested top-K metadata.
For large acceptance/debug runs, use `view=diagnostics`: Awoki serializes global
retrieval/stage telemetry before hits and omits source previews and repeated
parser/score payloads. The complete bounded candidate pool is retained as a
short-lived project-scoped metadata-only `columns+rows` trace rather than being
inlined into one MCP response. `diagnostic_targets` inlines complete records for
named deep candidates; `code_diagnostics_trace` pages or target-looks-up the full
trace. The primary response also contains a compact reranker-selected-window
summary. Reserved-lane eligibility, admission signals, ordering, and explicit
exclusion reasons are exposed without changing ranking behavior. Existing
concrete children requalified by refinement are protected across bounded candidate
composition without changing their fused score/rank, so they cannot disappear
before focus/refinement selection. Diagnostic targets also expose compact stage
presence across FTS, Qdrant, fusion, post-refinement discovery, and the composed
pool. Diagnostic traces are bounded/TTL-managed in process memory, contain no
source previews, and never become persistent project/backup state. Never scrape
OpenCode's local tool-output cache to reconstruct results.

Lexical and semantic freshness are published separately. The semantic snapshot
retains the last successful membership hash and `published_vector_collection`;
local/parser/FTS rebuilds with identical chunk membership preserve it. Source or
chunk membership, branch/repository identity, semantic embedding identity, or
collection identity changes make vectors stale; batch size, timeout, and retries
do not.

Repository provenance is reported as `VERIFIED_SNAPSHOT`,
`WORKING_TREE_BOUND`, or `FILESYSTEM_BOUND`. Reduced assurance never hides
readable source. Git author/committer metadata remains an unverified claim unless
a separate explicit signature trust operation establishes more. Replace refs,
sparse checkout, submodules, configured filters, `assume-unchanged`/manual
`skip-worktree`, weakened Git stat-trust configuration, and other unusual Git
view state are disclosed/lower assurance rather than silently changing the
evidence universe. Passive Git reads disable fsmonitor and neutralize configured content
filters instead of executing repository/local helper programs.

Use `/definition`, `/callers`, `/callees`, `/code-path`, or explicit `/code-across`
scope when precision matters. Use `/code-validate-claim` when a strict atomic
verdict is useful underneath the broader investigation. The slash command may
discover and decompose a broad verification request, but the underlying
`code_validate_claim` MCP tool accepts only strict atomic proof obligations and
never uses embeddings or reranking as evidence.

Use `code_semantics_check` for supported deterministic Go primitives instead of
guessing language/runtime behavior: path join/clean, duration parsing and
multiplication, failed `error` type assertions, bounded `strings.Replace`, `url.Parse`, and `httputil.ReverseProxy` Rewrite-entry forwarded-header behavior. Docker runs a small fixed stdlib-only helper precompiled by the pinned Go builder stage; source-tree development may compile the same fixed helper locally. It never compiles/executes repository code and has no network path. It reports the helper toolchain and the attached project's plain-text `go.mod` declaration; a mismatched Go major/minor is an explicit boundary for version-sensitive stdlib claims.

Choose lexical tools by intent. Use Awoki indexed/structural search for conceptual
discovery, OpenCode `Grep` for ordinary exact string/symbol lookup, and Awoki
`code_exact_search` when full ripgrep-style control materially helps complex/exhaustive
exact enumeration without Bash. `code_exact_search` is legitimate normal-work tooling
and does not need semantic retrieval to fail first. If structured exact-search output
errors/truncates or cannot establish the coverage required
for a claim, use the session-aware `code_text_search` MCP primitive and paginate
until `scan_complete=true`, then `search_complete=true`, with
`repository_universe_complete=true`. It materializes completed discovery so cursor
continuations do not rescan the repository; only pages and previews are bounded.
Acceptance contracts may deliberately restrict native tools and override this normal-work policy.
The internal
`.harness/bin/code-search-fallback` is the MCP-unavailable diagnostic equivalent.
Fallback output is discovery only.
`project_search` is broader; capture reconciliation is restricted to prior
continuity records so code chunks cannot masquerade as memory.

## Backup boundary

Use `make backup-portable` for installation migration and read `docs/BACKUP_RESTORE.md`. Backup/restore must remain quiescent by default, checksum-verified, traversal-safe, and fail-closed on overwrite. Do not include `.env`, SSH keys, or OpenCode state without explicit user direction. Full mode includes raw Qdrant/index state and therefore requires every Awoki service stopped plus compatible vector, general/code collection, provider/model label, normalisation, actual deployment identity, and Qdrant image settings. Restore never runs live. Checksums are integrity checks, not signatures.


## Evidence roles and optional adapters

Repository tests/fixtures remain searchable evidence. Generic behavior discovery softly prefers production source; explicit test/edge-case questions promote test evidence. Burp is optional: generic project creation/opening does not create `artifacts/burp/`, and generic global/project recall does not import Burp inventories. The Burp tree is created lazily after an explicit Burp preservation/write action.


## SSH runtime environment diagnostics

The OpenCode SSH entrypoint starts one authenticated OpenCode Web backend by default and supervises it alongside `sshd`; `awoki-opencode` attaches the SSH TUI to that shared backend. The generated Web password is not part of the general runtime snapshot: it is persisted only in ignored host `.opencode-state/web-auth/password` (0600), mounted read-only, and copied into `/run` tmpfs as `op:op 0600` before server start. The entrypoint separately snapshots only allowlisted runtime values into root-owned, mode-0640 `/run/awoki/runtime.env` on tmpfs because `sshd` does not propagate the Compose environment to login shells. `mcp-auto` validates the snapshot and relaunches Awoki MCP through the clean internal `mcp` profile rather than carrying arbitrary SSH/OpenCode environment. Normal skills stay MCP-mediated and must not source/print the file. Explicit shell diagnostics use `awoki-runtime-env` profiles: `base`, `qdrant`, `retrieval`, `burp`, `lavish`, internal `mcp`, or `all`. `retrieval`/`mcp`/`all` may pass API credentials and therefore are only for trusted diagnostics; Burp/Lavish tooling uses non-retrieval profiles. Profiles limit inherited variables but do not sandbox hostile same-user processes; the current stdio-MCP runtime user can deliberately read the tmpfs snapshot. `make runtime-config`, `make embedding-benchmark`, and `make reranker-benchmark` work from host or SSH container and never send repository source in the synthetic benchmarks. Passive Git/`rg` child processes strip retrieval/provider credentials and ambient loader/interpreter/SSH-agent overrides; the deterministic Go semantics helper already uses a fixed clean environment.

## Awoki self-development boundary

The hardened OpenCode SSH runtime is an analysis/runtime appliance, not an
implicit Awoki development checkout. Before any request to modify Awoki itself,
run `.harness/bin/awoki-dev-preflight` (or `make dev-preflight`) and require it to pass; it verifies the current root is the writable top-level Awoki Git checkout (`.git`
present, `git rev-parse --show-toplevel` resolves to that root, target files are
writable without privilege escalation). If `/awoki` has runtime source but no
top-level `.git` or is read-only to `op`, stop and report the environment
mismatch. Never try `sudo`, `su`, `chown`, `chmod`, `/root` discovery, or ownership
workarounds to convert the hardened runtime into a development environment; use a
separate writable host/dev clone.
