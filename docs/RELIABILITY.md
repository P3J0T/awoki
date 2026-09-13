# Awoki Reliability Model

Awoki uses progressive rigor. Reliability is not a single prompt and it is not a promise of infallibility.

## Review findings before relying on them

Say “review these findings before I act”, or use `/verify these findings`. Awoki reuses
the reliability ledger, evidence references and normal continuity; it does not
create another memory system. Casual notes and exploration need no formal review.

`reliability_status(view="review")` is a read-only, paginated projection:

- `MACHINE_CHECKED` / `MACHINE_REFUTED`: only the exact `checked_scope` in the
  receipt, at capture time. Analyst labels, broader conclusions and current source
  freshness are **not** certified. Legacy receipts without an exact scope need
  re-verification before receiving either label.
- `EVIDENCE_ATTACHED_UNVERIFIED`: evidence exists in the recorded assessment;
  attachment does not establish the truth of its interpretation. This view does
  not recheck evidence integrity; the existing checkpoint performs that check.
- `UNKNOWN`, `STALE`, `CONTRADICTED`: retained boundaries, not silently converted
  to success. Assessment contradictions are recorded judgments, not machine proof.

The assessment checkpoint's legacy `VERIFIED` result still means its graph and
evidence requirements passed—not that arbitrary behavior was proved. Reviews
inspect recorded findings; they cannot automatically discover every missing gap.
Important omitted alternatives still require deliberate or independent review.

Finalizing saves an ordinary report artifact with qualifications. Follow-up notes
using `based_on` keep that report source and its caveats. Exact memory reads show
a link back to the current review. The link grants no verdict to a paraphrase;
reopen the report and relevant source after compaction or source changes. The
review hash identifies the recorded items, not a signature or a truth certificate.

## Permanent invariants

Every mode keeps these minimum rules:

- Treat model output and remembered conclusions as fallible.
- Verify concrete source, configuration, runtime, and test claims against observable evidence.
- Never claim a check ran unless its result was observed.
- Separate observation, inference, and hypothesis.
- Preserve corrections, contradictions, uncertainty, source references, and scope.
- Do not silently expand scope or perform delivery actions.

These are reinforced through the compact `AGENTS.md` and shared `.harness/AGENT_CORE.md`, the continuity plugin at compaction boundaries, and Awoki code checks. This detailed document is loaded on demand for verification/reliability/acceptance work, not on every turn. The plugin reads the same installed core policy used at startup and deliberately does not append additional system messages: strict Qwen/llama.cpp chat templates may reject multiple or non-leading system messages. Prompts cannot guarantee semantic truth; hard storage and gate-result properties belong in code.

User-facing wording stays simple: **“verify your findings before answering”** means
reopen/verify the important evidence and do not state a required conclusion as fact
when it remains unsupported, stale, refuted, or contradictory. Internal claim-state
names are an implementation detail, not required prompt vocabulary.

## Progressive operating modes

### Remembering without manufacturing certainty

`project_capture` stays flexible: observations, user preferences, decisions,
hypotheses and unfinished work need no formal claim gate. For code-backed notes,
pass `sources=["ev_..."]` from `code_source_window` and put limitations in
`uncertainty`. Awoki fills missing repository/path labels from that saved source
artifact; it never guesses them from prose.
Source objects may use `id`, `ref`, or the source tool's own `evidence_ref` field.
Conflicting handle aliases fail closed instead of selecting one as current.

Capture validates explicit `ev_` references before saving or semantic duplicate
reconciliation. Missing, corrupt, wrong-project and mismatched source labels or
ranges reject that note with `invalid_sources` and a recovery hint. Generic valid
evidence is allowed; an intact but stale source artifact remains historical
evidence, not current behavioral proof. Earlier invalid notes remain readable as
unverified history, but cannot be propagated by restatement; an explicit correction
with recovered references is required. No fuzzy reference replacement is performed.

An exact restatement retains existing sources and caveats. New caveats, confidence,
state, tags or privacy changes append a revision and retire the old active record;
history is not rewritten. Omitted confidence preserves the previous value (new
notes default to medium). A resolved caveat requires an explicit `correction`
with `supersedes`, not another restatement that simply omits it. Different kinds
or repository scopes are not automatically merged.

Paraphrased follow-ups may use `based_on=[cont_...]`, in single or batch capture,
with at most three active safe notes from the same project. Their source bindings
and caveats are carried forward, and confidence cannot exceed the least-confident
parent. This appends an ordinary note with lineage metadata, not a new claim store;
the earlier notes remain active. Additional evidence/caveats may be supplied.
Missing/private/retired parents and invalid inherited evidence reject the write;
an intact stale source remains historical, visibly stale at recall. More than
100 combined sources/caveats rejects rather than truncates inheritance.
Explicit correction + supersedes, without based_on, is the path for resolving a
limitation. Similarity gives an advisory only; it never assigns a parent or proves
that the new wording follows from the old evidence. Private notes are not copied
into public derivatives, and casual independent notes remain unrestricted by this
optional linkage.

`project_search` and resumed knowledge expose per-record `source_freshness`:
`current`, `stale`, `unverified` or `unbound`. At most 16 distinct source checks run
per recall; excess references are explicitly not checked. Missing/wrong-project
handles fail closed. Plain paths, external references and user statements remain
valid memory but are not automatically verified. Checks are read-only and cached
only for that call; they do not refresh an index or contact a model. Generated
handoffs carry caveats and warn that saved sources need rechecking. Nothing here
sets a remembered behavioral claim to verified, or guarantees that a model saved
all important qualifications in the first place.

Optional `project_capture(items=[...])` saves 1–12 independent notes through the
same canonical journal. Each item has a short `summary`, optional `details`, its
own `sources` and `uncertainty`, and the ordinary kind/confidence/state fields.
Top-level name/privacy apply to the batch. Shape errors are rejected before any
write; semantic review or runtime failures can leave partial progress. This is
not a transaction. Inspect per-item results; no automatic semantic splitting or
claim verification is performed. Ordinary single-note capture remains available.
The typed item schema lists supported fields. `evidence_refs=[ev_...]` is accepted
on both single notes and items and merged into `sources` before validation; an
invalid handle cannot silently degrade into a plain citation. Privacy settings
belong to the outer batch. `source_binding` reports bound/mixed/unbound separately
from source freshness and claim truth.
Numeric item-size bounds are enforced server-side but omitted from the inline
model-facing schema for local grammar-builder compatibility; field types and
supported fields remain explicit. This does not relax capture validation.

Reopen saved source with `code_source_window(evidence_ref=ref)` (or the returned
`reopen_call`). It resolves the exact project/repository/path/range, rechecks source
identity, and returns bounded current bytes only while the reference remains
current. Missing, corrupt, wrong-project or stale handles fail explicitly; no
fuzzy lookup, scope widening or automatic reindex occurs. A citation display label
is not a repository-relative path. Ordinary path/range reads remain available.

`project_search` merges repeated continuity IDs across retrieval backends and
hydrates them from active safe canonical records. Search previews explicitly
indicate omitted details/caveats and provide exact `next_calls`. Recover complete
notes with `project_search(record_ids=["cont_..."])` (up to three at a time).
Long fields have section pages: `details`, `uncertainty`, `sources`,
`likely_continuation`, `tags`, or `supersedes`, with returned offset/character
limits. Exact recall makes no embedding/reranker request and performs no index
refresh. Missing, private or superseded records are unavailable on this safe
recall surface. History remains unchanged. Neither an exact read nor a current
source label authorizes a broader behavioral conclusion.
Every page, including a details-only read, also carries bounded `qualifications`:
known caveats, their count, an explicit read-all call when shortened, and the
source-binding boundary. Empty uncertainty is labeled not recorded, not proof
that none exists. `followup_capture` provides the exact parent ID for reuse.
Qualification pagination is separate from the requested section's `next_calls`
so draining a section cannot loop through another section's caveats.

### Retrieval input and failure budgets

Cross-project retrieval requires 1–8 exact project IDs; the public tool rejects
`all_indexed=true` before enumeration/backend calls. Default OpenCode configs ask
for native approval of `awoki_cross_project_code_search`. Prefer approval **once**
for the named request: a broad Always/allow/auto-approval override may authorize
future cross-project calls too. A model-generated target list is not consent.
Ordinary multi-repository work inside one project uses `codebase_search`, without
this extra approval. Normal cross-project responses use the same compact ranking
projection; full/diagnostics keep original details. This is not a global access
sandbox: alternate clients must enforce consent, and other tools/native filesystem
access retain their own permissions. Existing operator overrides take precedence;
upgrades must not silently overwrite user configuration.

`AWOKI_RERANK_MAX_INPUT_TOKENS=512` limits each query/document pair, in addition to
the existing document-character ceiling. By default Awoki uses a conservative
UTF-8-byte estimate with space reserved for special tokens. This is **not** an
exact token count and may clip more than necessary. Original search hits and
source windows remain intact; `reranker.input_budget` reports clipped documents.
An oversized query falls back explicitly rather than silently changing the query.

For accurate local pair counting, an operator may install the optional Hugging
Face `tokenizers` package in a custom runtime image and set
`AWOKI_RERANK_TOKENIZER_PATH` to an absolute, read-only `tokenizer.json` path inside
that runtime. It must match the provider's actual tokenizer and special-token
processing; a generic model alias cannot identify those. No weights or tokenizer
are downloaded automatically. A missing/invalid configured tokenizer is an error,
not an invisible switch to an estimate. Set the budget to the provider's supported
pair limit, then recreate the runtime to apply `.env` changes.

HTTP 429 responses install a process-local cooldown (1–60 seconds), respecting
numeric `Retry-After` within that bound. Subsequent interactive embedding/rerank
calls fail fast during it; there is no added retry loop. Existing configured
embedding SDK retry limits still apply to the first call. Key rotation uses a
different cooldown identity. Failure metadata separates rate limits, rejected
input and provider errors without returning credentials, request text or response
bodies. Fallback is observable; `strict_backends=true` still requires real backend
success. This does not control OpenCode's chat-model retry policy.

### Explore

Default for reverse engineering, Burp investigation, unfamiliar repositories, research, and scratch implementation.

Explore permits incomplete work, hypotheses, partial notes, and uncertain direction. It does not require a clean Git tree, tests, or a formal completion contract. The permanent invariants still apply.

### Verify

A focused evidence pass over important claims.

Typical checks:

- reopen load-bearing files, logs, artifacts, or live tool state;
- search for contradictory project memory;
- distinguish confirmed facts from inference;
- run the smallest relevant test or command;
- state what remains unverified.

For Burp-derived claims, load `burp-workflow` and use direct Burp MCP for live state.

### Reliability check

A local, adaptive gate invoked by `/reliability-check` or equivalent natural language. It first defines the claim being validated.

For code, it normally examines the diff, discovers repository-native build/test/lint commands, runs relevant checks, reviews error/security implications, verifies documentation or migrations, and records unexercised paths.

For reverse engineering or research, it checks evidence coverage, contradictory findings, reproducibility, uncertainty, privacy/index boundaries, and resume quality.

For unfinished work, a valid result may be “reliably paused” rather than “complete.”

A reliability result may be reported as passed only when every check marked required was actually observed to pass. The `reliability_start`, `reliability_record_check`, and `reliability_finish` tools persist that ledger and deterministically prevent failed or missing required checks from becoming `passed`. For load-bearing factual conclusions, the run may also record structured atomic claims. Use `reliability_verify_code_claim` or `reliability_verify_semantics_claim` when the corresponding deterministic verifier applies; `reliability_record_claim` records unsupported/inferred claims without allowing the model to self-certify `VERIFIED`. Contradictory or refuted verified claims fail the claim gate. Missing or unavailable checks and unsupported conclusions must be reported explicitly.

`/reliability-check` never pushes, creates a pull request, or contacts CI.

### Evidence-aware self-verification

Awoki uses bounded self-verification rather than an open-ended reflection loop. `reliability_record_assessment` stores an extensible epistemic graph of concise claims, hypotheses, observations, questions, contradictions, gaps, decisions, and deliberately non-gating `note` nodes. Natural-language statements and short analysis summaries remain flexible. The strict boundary is stable identity, authority class, evidence references, first-class relations, required-claim contracts, and lifecycle. Rich security/reverse-engineering reasoning is therefore a **structured spine, not a semantic straitjacket**.

Authority classes distinguish `tool_evidence`, `source_evidence`, `user_supplied_evidence`, `environment_observation`, `runtime_observation`, `analyst_observation`, `model_inference`, `external_reference`, and `legacy_observation`. Rich tool/source material remains in content-addressed non-RAG `ev_...` artifacts; assessment nodes carry only bounded summaries and stable evidence references. Notes may preserve investigation context without participating in the verification gate; material can later be promoted into a claim/hypothesis/question with explicit provenance.

Start important runs with an explicit subject contract: `subject`, `required_claims`, `required_properties`, and `corrective_budget`. A declared required claim cannot disappear by being recorded as optional. Each declared `required_property` is materialized as an ordinary required check, so flexible property wording stays allowed but the obligation cannot silently disappear from finalization. If no required structured claims were declared or recorded, the structured claim gate is `NOT_APPLICABLE`, never a vacuous success. Relations are canonical first-class records created with `reliability_record_relation`; the older embedded `relations` input remains a compatibility projection.

Missing, stale or inconclusive **required** structured claims block completion in
reliability mode as well as ship mode. Exploration still needs no formal gate,
and check-only runs may pass without claiming behavioral proof. Returned/saved
reports include `reporting_boundary`, separating run status, structured claim
verification and assessment state. Final answers must disclose verifier errors,
inconclusive results and unperformed checks; source freshness alone does not
verify a behavioral conclusion. This boundary also accompanies continuity capture.

`reliability_verification_checkpoint` deterministically checks referenced artifact integrity, graph coherence, required unresolved gaps/contradictions, deterministic claim receipts, and explicitly requested backend properties. Its result taxonomy is `VERIFIED`, `VERIFIED_WITH_FINDINGS`, `INCOMPLETE`, `CONTRADICTED`, `BLOCKED`, or `NOT_APPLICABLE`. Backend degradation is always reported as a finding. It blocks only when the assessment explicitly requires the degraded capability, e.g. `requirements=["reranker_complete"]`; this prevents an unrelated TEI timeout from invalidating a structural/source claim. Cross-source evidence is allowed by default and becomes fail-closed only when a node explicitly requires `single_evidence_scope`.

When a required checkpoint is incomplete or contradicted, call `reliability_consume_corrective_budget` **before** the one safe, high-value corrective retrieval/verification action, then run one final checkpoint. Checkpoints themselves never consume budget. The run must stop rather than recursively self-criticizing, repeating the same failing call, widening scope, or mutating source/configuration to make the claim pass. `VERIFIED_WITH_FINDINGS` means the required mechanics/provenance passed while non-load-bearing findings remain; it does not turn model inference into machine proof.

Reliability mechanics and retrieval acceptance are separate ledgers. `reliability_aggregate_verdict` reports each component verdict plus an explicit overall result so a passing verification-mechanics run cannot be mistaken for a passing acceptance suite.

### Ship check

An explicit delivery workflow invoked by `/ship-check`. Start its ledger with `mode="ship"`; this activates the fail-closed structured-claim gate. Every required claim must have a machine-verifier receipt. `INCONCLUSIVE` or `STALE` required claims block shipping, `REFUTED` or `CONFLICT` claims fail it, and a model-authored `VERIFIED` status without a receipt is downgraded rather than trusted. If a broad conclusion cannot be represented by an available deterministic verifier, narrow the required ship claim to what can actually be proven and report the remaining inference separately instead of manufacturing certainty.

It builds on a local reliability check and may use no-mistakes when installed and compatible. Push, pull-request, CI, publish, or release actions require explicit user authorization. The absence of a remote must not prevent a local reliability result.

## Reliability report

Store durable reports under the active project when useful:

```text
reports/reliability/<timestamp>-<subject>.md
```

A report should include:

- claim or intended result;
- scope and relevant diff/artifacts;
- commands/checks actually executed;
- observed results;
- evidence references;
- unresolved risk and untested paths;
- status: passed, failed, blocked, or reliably-paused.

Do not infer a pass from the model’s opinion. A failed required check cannot be represented as passed.

## Lifecycle reinforcement

`session_runtime_status.current_turn_complete` requires a terminal text response
attributed to the current user message. New user turns invalidate prior completion
immediately, even if interruption prevents an idle event. Summary completion is
recorded separately. Tool-only/denied, provider-error, length-limited, empty and
unattributed turns cannot become completed. This is structural completion only:
CLI exit, tool success, factual correctness and task success remain separate.

OpenCode loads the reliability rules through `AGENTS.md` and `.harness/AGENT_CORE.md`. During context compaction, the continuity plugin reads that same core, followed by bounded work/acceptance/reference state before generated project prose. Awoki operation names remain MCP interfaces; normal repository work may use OpenCode Grep/`code_exact_search` for exact lexical tasks while Awoki indexed search remains the conceptual-discovery path. Acceptance-specific instructions are injected only when acceptance continuity exists: recover the exact current durable contract through `acceptance_run_next`, whose native-tool restrictions override normal ergonomics. It does not use `experimental.chat.system.transform` or append another system message, preserving compatibility with strict local-model chat templates.

For multi-step acceptance or benchmark work, compaction-safe reporting uses `acceptance_run_*` v4. Each test may carry a bounded machine-checkable protocol contract (required execution interfaces, required acceptance-orchestration interfaces, required scalar observations/pass conditions, evidence capture scope, native-tool restrictions/counts, optional execution/orchestration invocation ceilings, forbidden tool classes, and stop boundary). The plugin records execution provenance separately from acceptance scheduler/status provenance, without arguments/results/source/reasoning; record/finalize controls cannot satisfy their own test. `acceptance_run_record` persists the observation immediately and may downgrade a claimed PASS when those machine-observable contract conditions are not met. It returns the immutable `aat_` ID/effective outcome for the new attempt plus bounded immediately-prior attempt context. Optional `prior_attempt_requirements` are evaluated against machine-owned history (`count`, `exists`, and the immediately prior attempt's ID/number/claimed/effective outcome), so bookkeeping corrections can require a prior INCOMPLETE attempt without predicting the effective outcome of the attempt currently being recorded. Stable `ev_` content identity is independent from a bounded non-RAG sidecar that records which acceptance runs captured the artifact, enabling current-run evidence requirements. Compaction generation/count plus a bounded trigger-classified compaction-event history are durable, and the exact current contract is reinjected after automatic as well as manual compaction. The final report aggregates `acceptance_run_status`, so exact earlier ranks/scores are not reconstructed from a compaction summary. The run is bound to its managed source revision and published vector membership; drift fails closed. This still does not turn model inference into deterministic proof. Raw source text and raw tool output are not stored in the compact ledger.


Human navigation is deliberately separate from epistemic authority. Stable IDs remain
authoritative, while `reference_describe`, `reference_annotate`, and `reference_resolve`
provide compact labels, `why_saved`, aliases, origin/scope, and linked refs. The catalog is
non-RAG control-plane state; natural-language resolution is only a way to find the exact
ID that must then be used for evidence/state retrieval. Natural-language resolution is deliberately ambiguity-safe: close/low-confidence matches return no resolved stable ID. `cand_` descriptions distinguish first materialization from later evidence occurrences, and `aat_` describes immutable acceptance attempts so intermediate machine downgrades remain auditable. Acceptance bookkeeping corrections should use the prior-attempt fields returned by `acceptance_run_record`, `prior_attempt_requirements`, or the stable prior `aat_` directly; a pass requirement must not assume the not-yet-computed effective outcome of the current attempt.

## Repository evidence reliability

Repository answers use layered evidence rather than trusting one mechanism. `code_index_status` is passive; `code_index_verify` performs the deeper repository/source audit. `code_source_window` binds the exact returned bytes/range to a compact checksum-protected evidence ID, and `code_evidence_verify` detects later byte or snapshot/view drift. Evidence IDs are not signatures.
`VERIFIED_SNAPSHOT` binds the declared Git/indexed source view; it does not prove Git-ignored untracked files are absent. Use explicit ignored-file forensic search when that scope matters.

Git provenance is intentionally bounded: exact root/HEAD/tree and mutable view state can be established locally, while author names remain unverified metadata and hidden/rewritten remote history cannot be disproved without an external anchor. Shallow/grafted/replaced history and partial-clone state are disclosed. Passive Git reads disable fsmonitor and lazy promisor fetching and neutralize configured content-filter helpers; signature verifier programs are not invoked automatically. Freshness now has two Git identities: the **content-view fingerprint** binds HEAD plus content-selection state such as replacement refs and sparse-view patterns, while the broader **repository-view fingerprint** also records mutable index identity and stat-trust configuration for assurance diagnostics. Metadata-only repository-view drift with an unchanged clean content view is reported as `repository_view_metadata` and does not masquerade as corpus staleness. Deep verification still hashes the index bytes and source/document set, and it inspects `assume-unchanged`/manual `skip-worktree` flags. `core.ignoreStat=true`, `core.trustctime=false`, or `core.checkStat=minimal` lower passive assurance instead of being accepted as a strong clean-tree proof; those assurance failures can block passive reuse without claiming that the corpus revision itself changed. Provenance assurance is separate from cache freshness: stable sparse/submodule/replacement-ref views may reuse their already-materialized visible-source index while still remaining `WORKING_TREE_BOUND`, while changed content-selection identity remains fail-closed. Reduced assurance never removes otherwise eligible source.

For supported deterministic Go primitives, `code_semantics_check` is the reliability escalation path. Docker executes a small fixed stdlib-only helper precompiled by the pinned Go builder stage; source-tree development may compile that same fixed helper locally. Repository code is never compiled/executed, the helper has no network path, and target `go.mod` versus helper-toolchain alignment is reported for version-sensitive operations.
