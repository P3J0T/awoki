# Awoki Harness Map

Awoki is a continuity-first OpenCode harness for long-running evidence-backed software/security investigations: scoped project state, structural/lexical and optional semantic retrieval, durable evidence references, bounded verification, and compaction-safe continuation.

v0.1.7 is the current public patch release in the stabilization/usefulness-evaluation line. For the dense maintainer identity and “what not to add casually” rules, read `docs/AWOKI_IDENTITY.md`; do not inject that dense file wholesale into normal user tasks.

## Core components

```text
OpenCode          remote LLM endpoint, native tools, commands, skills, plugins
Awoki MCP         continuity, memory, indexing, project state, evidence adapters
SQLite FTS        project-memory lexical retrieval plus a separate structural code index
Qdrant            separate general-memory and content-addressed code-vector collections
Embedding API     configured remote OpenAI-compatible endpoint
Reranker          optional remote HTTP final ordering
Burp MCP          direct live Burp control plane
```

No conversational or embedding model weights are stored in the Awoki container.

## Continuity invariant

`docs/CONTINUITY.md` is authoritative.

Projects live under `workspace/projects/<project_id>/`. Their canonical continuity stream is `memory/continuity.jsonl`; `SITUATION.md` and `HANDOFF.md` are generated bounded views. Goals, task lists, pending items, and Burp state are optional facets rather than project requirements.
A plain save/remember/note is a neutral observation by default. Stronger labels such as finding, discovery, decision, or correction are used only when they add semantic meaning. Evidence fields are optional for ordinary facts/observations; high-confidence findings/discoveries are the evidence-oriented case. Durable continuity IDs remain internal references for supersession and diagnostics.

Preferred project tools:

```text
project_open
project_capture
project_search
project_refresh
project_pause
project_status
```

They are Awoki MCP tool names, not executable shell commands. Natural-language
requests should route to these tools directly. Do not synthesize commands such as
`awoki_project_refresh`, and do not use a transient Bash `export` to configure an
MCP process that is already running.

Repository membership is managed separately with `project_repo_add`,
`project_repo_list`, `project_repo_remove`, and `project_repo_default`. Natural
language such as “add repo oathkeeper” should infer `repo/oathkeeper`; registration
verifies an existing Git checkout is the exact top-level when Git-backed and never
clones, moves, or deletes repository files. `project_open` and `project_repo_add`
return passive `repository_index_advice`; surface structural/vector freshness. When
an existing structural/FTS snapshot is stale, use detached `code_index_refresh_start`
first and report its job id; `code_index_refresh_status` exposes bounded local
file/parser progress and `code_index_refresh_cancel` is explicit-only. Local refresh
performs no remote embedding/Qdrant work. Offer `code_vector_refresh_start` only
when vectors remain stale, and never trigger remote embedding merely because a
project was opened or a checkout was registered. Both refresh classes run in
detached workers; report the job id and return control. Never autonomously poll
either in a loop.
The `repository-readiness` skill is the reviewed procedure for explicit "prepare/prime this repo" requests; it converges on `LOCAL_READY` or `FULL_READY` without silently changing retrieval configuration.

The stable user-facing command surface is documented in `docs/COMMANDS.md`.
`/project`, `/codebase`, and `/burp` are natural-language front doors. Exact code
operations and explicit Burp side effects remain separate; archive helpers and
low-level MCP tools are not duplicated as slash commands.

Repository-only search is exposed separately through `codebase_search` and
`/codebase`. It is intentionally not part of the six canonical continuity
operations because it enables and queries a source-code index rather than
creating or updating project continuity.

Conceptual retrieval is staged and authority-aware rather than test-blind:
FTS/current-Qdrant discovery preserves raw provenance, bounded verified graph
edges may add related production candidates, strong coarse production containers
may refine into concrete callables, and a focus-aware bounded selector ensures
important implementation/test/refinement candidates can enter the remote reranker
even when they fall below the global broad cutoff. The optional reranker evaluates
only that selected window against the original query, and a bounded
result-focus authority prior plus deterministic diversity produces final top-K.
Tests/config/schema/docs remain searchable and are preferred when explicitly
requested. A graph-connected production symbol is only a candidate; structural
connectivity never proves behavioral relevance. For diagnostics, real
`mode=lexical`, `use_fts`, `use_qdrant`, `use_reranker`,
`structural_promotion`, `result_focus=auto|implementation|balanced|tests|config`,
and `strict_backends` isolate stages. Unknown explicit modes are rejected and
reranker use is reported through requested/attempted/applied telemetry rather
than inferred from scores. Large acceptance runs use `view=diagnostics`: global
telemetry arrives first, exact `diagnostic_targets` can be inlined, and the full
bounded metadata-only candidate pool is stored behind a short-lived project-
scoped handle readable through `code_diagnostics_trace`. Trace reads are paged or
path/symbol targeted and never contain source previews. Requalified concrete
children are protected across the bounded composition step without score/rank
promotion so the focus/refinement selector, not an earlier expansion cutoff,
decides whether they receive reranker capacity. Target diagnostics expose compact
FTS/Qdrant/fused/post-refinement/composed-pool presence to localize earlier loss.
Explicit test/config role is not sufficient by itself for a reserved focus slot:
the candidate also needs independent lexical/backend relevance. Likewise, refill
capacity is no longer blind quota fill; low-evidence leftovers are skipped and
the selector reports rejected refill candidates plus any unused budget. Structural
declaration extraction preserves named owners through bounded declaration-wrapper
nodes while refusing parameter/body descent, keeping symbol identity precise
across supported grammar shapes.

The native code engine uses policy-approved repository bytes only, creates
Tree-sitter symbol chunks where supported, falls back deterministically where a
parser is unavailable, stores definitions/references/call edges in a dedicated
project SQLite database, and stores content-addressed vectors in a dedicated
Qdrant code collection. The active branch/worktree is the default scope. Exact
cross-project search requires an explicit project list or explicit all-indexed
scope.

Natural-language `/codebase` requests are evidence-backed repository
investigations by default, not retrieval-only answers. A project may retain the
legacy single Git root at `repo/`, or register multiple exact roots such as
`repo/oathkeeper` and `repo/hydra`. Broad discovery spans enabled registered
repositories; exact operations require a repository selector when ambiguous,
and evidence IDs bind the originating repository identity. Indexed search discovers
candidate files/symbols; semantic similarity is never behavioral proof. Once an
entry point is known, exact symbol operations plus the internal `code_flow_graph`
primitive build a bounded relevant reachable graph, traversing only resolved
calls and preserving ambiguous/unresolved boundaries. `code_source_window`
returns bounded, active-branch, hash-checked source for branch conditions,
assignments/aliases, arguments, returns, and outcomes; giant source lines are
explicitly clipped rather than emitted unbounded. Source windows also return a
portable evidence ID that binds the range to exact source bytes and repository
view identity. `code_evidence_verify` checks whether that evidence is still
current after edits or Git-view changes. Evidence IDs are checksum-protected
stale-detection tokens, not signatures or origin attestations.

Repository evidence has three normal assurance levels. `VERIFIED_SNAPSHOT`
means an explicit index/verify operation established the exact Git root, commit,
tree/view, clean materialized source, and no supported view anomaly that would
make the snapshot claim too strong. `WORKING_TREE_BOUND` means source is still
available and hash-bound but the whole worktree is not claimed as one immutable
snapshot. `FILESYSTEM_BOUND` covers intentional non-Git trees. Git author and
committer fields are recorded only as metadata claims; Awoki does not silently
turn them into verified human identity. Unusual Git state lowers assurance and
is disclosed rather than filtering source, including replace/sparse state,
submodules, active filters, `assume-unchanged`/manual `skip-worktree`, and weakened
Git stat-trust configuration.

Precision operations remain available through `/definition`, `/callers`,
`/callees`, `/code-path`, `/code-across`, and `/code-validate-claim`. The
validation command accepts natural-language source-logic requests, discovers the
relevant code, and decomposes broad requests into strict atomic obligations. The
underlying `code_validate_claim` MCP primitive remains deliberately narrow and is
used selectively underneath broader deterministic investigation. Verification
does not use embeddings or reranking as evidence and refuses certainty when exact
source and AST proof obligations are incomplete.

For deterministic Go language/stdlib primitives, `code_semantics_check` provides
an allow-listed language-native observation without executing repository code or
fetching modules. It covers path join/clean, duration parsing/multiplication,
failed `error` type assertions, bounded string replacement, URL parsing, and `httputil.ReverseProxy` Rewrite-entry forwarded-header behavior.
The result reports the actual helper Go toolchain and, when a project is attached,
its plain-text `go.mod` declaration so version-sensitive stdlib observations are
not silently treated as proof for a different target toolchain. Docker ships a
small helper precompiled by the pinned Go builder stage rather than the full Go
toolchain; source-tree development may compile the same fixed helper locally.

Normal repository work chooses exact-search tools by intent. Use Awoki indexed/
structural retrieval for conceptual or architectural discovery; use OpenCode
`Grep` for ordinary known string/symbol lookup; and use Awoki `code_exact_search`
when full ripgrep-style flags/counts/context/globs materially improve a complex or
exhaustive exact enumeration without Bash. `code_exact_search` is a first-class
exact-search tool and does not need semantic retrieval to fail first. If structured
exact-search output errors/truncates or cannot establish the coverage
required for a claim, use `code_text_search`: it scans the complete permitted
source scope, materializes discovery once, and serves later pages from that
snapshot. Follow `next_cursor` while `scan_complete=false`, then until
`search_complete=true`, and require `repository_universe_complete=true` before
claiming exhaustive repository-source coverage. Git-ignored untracked files stay
outside the default scope; explicit forensic searches use `include_ignored=true`.
Acceptance contracts may deliberately restrict native tools and override this
normal-work policy. All lexical output remains discovery-only.

The user’s current instruction overrides any generated continuation suggestion.

Agent-turn recovery is a separate runtime boundary. The OpenCode plugin observes only structural message metadata. It records `reasoning_only_terminal_turn` when a terminal assistant turn has reasoning but no normal text/tool part, and `tool_execution_without_followup` when a tool actually completed but the terminal assistant turn produced no normal text continuation. Query `session_runtime_status` for classification, finish reason, provider/model/agent identity, provider error class when present, completed-tool count, structural step-finish token counters, and current compaction generation. Private reasoning content is never persisted. Generic model-turn recovery is detection/reporting only: Awoki never auto-prompts "continue" for these turns, and a manual user follow-up is accounted separately from reliability corrective budgets.

For long acceptance suites, `acceptance_run_next` returns only the next unfinished planned test and its bounded allowed/forbidden action metadata. This reduces scheduler re-planning while leaving investigation claims, hypotheses, notes, and analysis semantically flexible.

## Retrieval

Awoki combines:

1. project-first SQLite FTS;
2. Qdrant semantic hits generated with the configured remote embedding endpoint;
3. JSONL scan fallback where applicable;
4. weighted reciprocal-rank fusion;
5. optional remote HTTP reranking.

Default configuration:

```text
AWOKI_EMBEDDING_PROVIDER=openai
AWOKI_EMBEDDING_MODEL=text-embeddings-inference
AWOKI_EMBEDDING_DEPLOYMENT_ID=jinaai/jina-embeddings-v2-base-code
AWOKI_EMBEDDING_BASE_URL=http://embedding.example.invalid:8000/v1
AWOKI_EMBEDDING_BATCH_SIZE=32
AWOKI_EMBEDDING_NORMALIZE=1
AWOKI_VECTOR_SIZE=768
AWOKI_QDRANT_URL=http://qdrant:6333
AWOKI_QDRANT_COLLECTION=awoki_jina_embeddings_v2_base_code_768
AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH=0
AWOKI_RERANK_ENABLED=0
```

The example TEI deployment fixes the real model as
`jinaai/jina-embeddings-v2-base-code`; `text-embeddings-inference` is the model
field sent to its OpenAI-compatible endpoint. Reranking can be enabled
independently with `AWOKI_RERANK_PROVIDER=tei`,
`AWOKI_RERANK_URL=http://reranker.example.invalid:8000/rerank`, and an empty
`AWOKI_RERANK_MODEL`. A reranker failure defaults to the existing fused order.

Retrieval scopes are deliberately separated:

```text
codebase_search          active-project repository code only
cross_project_code_search explicitly selected project repositories only
project_search           broader safe project knowledge
capture reconciliation  prior continuity records only
```

General project retrieval and structural code retrieval use different SQLite
schemas and different Qdrant collections. `retrieval_status` and
`code_index_status` are passive/local reads: neither performs a live Qdrant probe
or repository-wide verification. Use `retrieval_probe` for explicit bounded live
backend checks and `code_index_verify` for explicit deep source/code-vector
verification.

Qdrant is a derived retrieval layer. Continuity JSONL and source files remain authoritative and can rebuild it.

Project open and repository registration are passive with respect to remote code
embeddings. They return `repository_index_advice` describing structural/vector
freshness per registered repository. If an existing structural snapshot is stale,
the response first includes detached `code_index_refresh_start`; this keeps full
repository parsing outside the interactive MCP request deadline and reports bounded
file/parser progress through `code_index_refresh_status`. If semantic vectors remain
missing or stale afterward, the response includes the exact opt-in
`code_vector_refresh_start` action. The OpenCode project workflow should surface the
relevant recommendation immediately. These background jobs keep MCP responsive; do
not repeatedly poll either without a user request or a later action that needs
current state.

The vector-job protocol also returns/enforces a recommended polling cadence,
retains truthful partial progress on failure, and skips unchanged Qdrant payload
writes during full-reuse refreshes. Lexical and semantic publication are
separate: the last successful vector membership/collection remains authoritative
until source/chunk membership, repository/branch identity, semantic embedding
identity, or collection identity changes. Batch size, timeout, and retries are
operational and do not invalidate reusable vectors.

## Index boundary

Only project-policy-allowed material is sent to FTS, the remote embedding endpoint, Qdrant, or the remote reranker.

Excluded from semantic/structural indexing by default:

- explicit no-RAG records;
- raw Burp requests/responses and HAR/HTTP dumps;
- `.env` and private-key material;
- high-confidence credential values redacted best-effort at source/analysis write or transport boundaries without hiding the surrounding security evidence.

Coverage-first local repository text search is separate: textual sensitive/config files and tracked generated/vendor text may be counted and searched locally with opaque previews instead of disappearing. Explicit no-RAG markers remain the intentional user-controlled exclusion. Security names such as `auth`, `token`, `credentials`, JWT, OAuth, or `secrets/` never cause exclusion by themselves.

## Sensitive memory

Awoki has no built-in credential database or credential MCP tools.

Normal capture preserves security-analysis semantics and redacts high-confidence credential values best-effort. Redaction alone does not remove the surrounding finding from retrieval. When the user explicitly asks to preserve sensitive plaintext, use the explicit sensitive capture option. That record is stored append-only with secret sensitivity and `no_rag`, omitted from generated views and automatic retrieval, and returned only through an explicit sensitive search.

Awoki does not claim that generic memory storage is encrypted. Optional external credential skills may be added later under `.opencode/skills/`.

## Reliability

`docs/RELIABILITY.md` defines progressive rigor:

```text
/explore
/verify
/reliability-check
/ship-check
```

Always-loaded rules treat model output as fallible, require observed evidence for concrete claims, and prohibit invented test/runtime results. They are delivered through `AGENTS.md` and OpenCode `instructions`; the continuity plugin deliberately does not append extra system messages because strict Qwen/llama.cpp templates may reject them. The plugin preserves a bounded reliability reminder only inside compaction context. Reliability runs also use a durable ledger whose required-check status is enforced in code. Ship-mode runs additionally require structured claims with deterministic verifier receipts; unverified/stale claims block shipping, refuted or contradictory verified claims fail it, and the model cannot self-certify `VERIFIED`.

## Runtime backup and restore

`docs/BACKUP_RESTORE.md` is authoritative. `make backup-portable` captures canonical workspace, continuity, artifacts, configured global roots, and skills while excluding derived indexes and credentials. `make backup-full` additionally captures SQLite/project indexes and stopped Qdrant storage and requires all Awoki services quiescent. Both create `0600` archives with `.sha256` sidecars. `.env`, `.ssh-container`, and `.opencode-state` require explicit opt-in; named Docker volumes are recreated. Restore verifies archive paths/checksum, refuses every live Awoki service or existing data by default, stages completely before destructive force actions, regenerates installation-specific layout state, and rebuilds lexical indexes after a portable restore.

## Burp

Direct PortSwigger Burp MCP remains the live control plane. Burp is an optional adapter: ordinary projects do not receive Burp artifacts or Burp inventories in generic recall. `artifacts/burp/` is created lazily only after explicit Burp preservation/write activity. `/burp` routes natural-language inspection, searching, summarization, archive lookup, and safe preservation. `/burp-repeater`, `/burp-intruder`, and `/burp-send` remain explicit because they represent distinct side-effect intent. Awoki stores compact project observations, safe summaries, and continuity; raw traffic remains outside broad RAG.

Inside Docker, Burp on the macOS host is reached at:

```text
http://host.docker.internal:9876
```

No host networking or Docker socket is required.

## Container storage boundary

Awoki source is copied into the image and is not bind-mounted in normal operation. Writable mounts are limited to:

```text
/awoki/workspace
/awoki/.harness/state
/awoki/.harness/index
/awoki/.harness/artifacts
/awoki/.harness/memory
/awoki/.harness/notes.md
/global
OpenCode and Neovim state directories
SSH authorized key and server-host-key volume. Host initialization creates the authorized-key bind source as a regular file before Compose starts; the long-form bind disables host-path auto-creation, and the launcher repairs only the legacy safe empty-directory failure shape while refusing symlinks or non-empty directories.
```

Qdrant data remains in `./data/qdrant`; initialization explicitly creates `data/qdrant/collections/` so collection materialization has a valid parent on bind-mounted filesystems. Qdrant joins only the internal `awoki-data` network; Awoki/OpenCode also joins `awoki-egress` for remote embeddings, reranking, and host Burp MCP. Neovim and tmux are installed; the shipped Neovim configuration loads no third-party plugins.


The OpenCode SSH image pins Node 22 for OpenCode and ad-hoc Lavish compatibility. The preferred runtime starts one authenticated OpenCode Web backend by default and uses `awoki-opencode`/`opencode attach` for the SSH TUI so browser and terminal share sessions. Web is host-loopback-only; its generated password is outside Compose/runtime snapshots and is copied from a 0600 ignored host file into `/run` tmpfs.


## Generic long-running task continuity

Use `project_task_checkpoint`, `project_task_status`, and `project_task_finalize` for generic code, document, research, and repository-analysis work. Burp task tools are reserved for live Burp workflows. OpenCode compaction/session continuity is independent from code indexing; task checkpointing never implies a repository reindex. Detached-job `project_continuation_*` self-resume is the only automatic conversation wake-up path: it is limited to managed project scope, one-shot polling timers, a claim lease, a 48-hour lifetime enforced again at claim time, and three resume claims across one active chain. Rescheduling an unfinished chain preserves its deadline and consumed claim count; only a new schedule after terminal finalize/cancel/block starts a fresh bound. A failed/blocked job, scope conflict, expired lifetime, or exhausted claim budget stops automatic continuation. Generic assistant-turn anomalies never use this wake-up mechanism; they are structural detection/reporting only via `session_runtime_status`.

OpenCode's native TODO list is also mirrored into a bounded session-local work ledger under `.harness/state/work-ledger/`. For a multi-step natural-language review, that TODO projection is the preferred small active working set for the user's requested deliverables/constraints across compaction; it should not copy the raw prompt or every intermediate thought. The same ledger tracks only current-session human reference usage, so old project references remain searchable without being injected by recency alone. This mirror is operational recovery state, including for unattached/ad-hoc sessions; it is not canonical project memory and never overrides a newer user instruction. `session_work_status` exposes it deliberately after compaction/restart when needed. The plugin marks older TODO/reference state for review on a new user turn and refreshes TODOs from `todo.updated`; it does not silently overwrite native TODO UI from an old mirror.

Multi-test acceptance/benchmark runs use `acceptance_run_*` v4, stored as bounded project artifacts under `artifacts/acceptance/`. The run captures the exact managed source/revision/vector-membership identity and starting compaction generation, rejects later scope drift, and persists each structured test observation immediately. A bounded test contract may declare required execution interfaces, required acceptance-orchestration interfaces, required observations/pass conditions, current-run evidence scope, native-tool policy/counts, per-interface invocation ceilings, forbidden tool classes, and stop boundaries; OpenCode contributes separate structural execution/orchestration provenance only. `acceptance_run_record` machine-checks those conditions and can downgrade a claimed PASS to `incomplete` or `protocol_deviation`. Compactions advance a durable generation/count plus bounded event history whose trigger is recorded from OpenCode's structural compaction signal as `automatic_context_pressure`, `explicit_request`, or `unknown` and the compaction injection carries the full current bounded test contract plus core MCP execution invariants, so an agent does not have to reconstruct PASS criteria from compacted prose. Stable `ev_` artifacts remain content-addressed while a non-RAG provenance sidecar records which acceptance runs captured them. Re-recorded tests retain bounded immutable `aat_` attempt history instead of overwriting prior machine outcomes; `acceptance_run_record` returns the new attempt plus bounded immediately-prior attempt context, and `prior_attempt_requirements` can machine-check immutable prior history, so correction drills can use already-computed outcomes rather than self-referential future-outcome requirements; candidate navigation distinguishes first materialization from later evidence occurrences; ambiguous natural-language reference phrases do not resolve to a stable ID. `harness_self_check` provides only allow-listed hermetic Awoki self-regressions and never arbitrary command execution. Raw source/tool output is not accepted into the compact ledger, and persisted model observations remain observations rather than machine proof.


## SSH runtime environment diagnostics

The OpenCode SSH entrypoint snapshots only allowlisted runtime values into root-owned, mode-0640 `/run/awoki/runtime.env` on tmpfs because `sshd` does not propagate the Compose environment to login shells. `mcp-auto` validates the snapshot and relaunches Awoki MCP through the clean internal `mcp` profile rather than carrying arbitrary SSH/OpenCode environment. Normal skills stay MCP-mediated and must not source/print the file. Explicit shell diagnostics use `awoki-runtime-env` profiles: `base`, `qdrant`, `retrieval`, `burp`, `lavish`, internal `mcp`, or `all`. The wrapper avoids Bash-4-only conditionals so host-side hermetic validation remains compatible with macOS system Bash 3.2. `retrieval`/`mcp`/`all` may pass API credentials and therefore are only for trusted diagnostics; Burp/Lavish tooling uses non-retrieval profiles. Profiles limit inherited variables but do not sandbox hostile same-user processes; the current stdio-MCP runtime user can deliberately read the tmpfs snapshot. `make runtime-config`, `make embedding-benchmark`, and `make reranker-benchmark` work from host or SSH container and never send repository source in the synthetic benchmarks. Passive Git/`rg` child processes strip retrieval/provider credentials and ambient loader/interpreter/SSH-agent overrides; the deterministic Go semantics helper already uses a fixed clean environment.

## Awoki self-development boundary

Before modifying Awoki itself, run `.harness/bin/awoki-dev-preflight` (or `make dev-preflight`) and require it to pass. It verifies the current root is the writable top-level
Awoki Git checkout: `.git` exists there, `git rev-parse --show-toplevel` resolves
to that root, and intended files are writable without privilege escalation. The
hardened `/awoki` OpenCode SSH runtime may intentionally omit the top-level
Awoki `.git` and be read-only to `op`; it is not a development checkout. If the
check fails, stop and report the environment mismatch. Never use `sudo`, `su`,
`chown`, `chmod`, `/root` discovery, or ownership workarounds to turn the runtime
appliance into a development environment. Use a separate writable host/dev clone.
