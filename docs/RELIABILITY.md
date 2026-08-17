# Awoki Reliability Model

Awoki uses progressive rigor. Reliability is not a single prompt and it is not a promise of infallibility.

## Permanent invariants

Every mode keeps these minimum rules:

- Treat model output and remembered conclusions as fallible.
- Verify concrete source, configuration, runtime, and test claims against observable evidence.
- Never claim a check ran unless its result was observed.
- Separate observation, inference, and hypothesis.
- Preserve corrections, contradictions, uncertainty, source references, and scope.
- Do not silently expand scope or perform delivery actions.

These are reinforced through `AGENTS.md`, OpenCode instructions, the continuity plugin at compaction boundaries, and Awoki code checks. The plugin deliberately does not append additional system messages: strict Qwen/llama.cpp chat templates may reject multiple or non-leading system messages. Prompts cannot guarantee semantic truth; hard storage and gate-result properties belong in code.

User-facing wording stays simple: **“verify your findings before answering”** means
reopen/verify the important evidence and do not state a required conclusion as fact
when it remains unsupported, stale, refuted, or contradictory. Internal claim-state
names are an implementation detail, not required prompt vocabulary.

## Progressive operating modes

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

OpenCode loads the reliability rules through `AGENTS.md` and the configured instruction files. During context compaction, the continuity plugin adds bounded reliability plus a small execution-invariant section before generated project continuity: Awoki operation names remain MCP interfaces; normal repository work may use OpenCode Grep/`code_exact_search` for exact lexical tasks while Awoki indexed search remains the conceptual-discovery path; and an active acceptance run must recover its exact current durable contract through `acceptance_run_next`, whose native-tool restrictions override normal ergonomics. It does not use `experimental.chat.system.transform` or append another system message, preserving compatibility with strict local-model chat templates.

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
