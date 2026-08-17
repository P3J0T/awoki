# Awoki Identity — Dense Maintainer / Future-Context Brief

**Release baseline:** v0.1.7
**Harness version:** 10.21
**Current phase:** stabilization, real-work usefulness evaluation, simplification before further feature expansion

This file is intentionally dense. It is for maintainers, future ChatGPT/OpenCode contexts, reviewers, and contributors who need to reconstruct Awoki's identity quickly without rereading the whole repository. It is **not** intended to be injected wholesale into every model turn.

## One-sentence identity

Awoki is a Docker-first OpenCode harness for long-running evidence-backed software/security investigations where repository identity, retrieval provenance, important analytical state, bounded verification, and continuation must survive compaction/restart without turning chat history or semantic similarity into truth.

## Design objective

Awoki should make this natural:

> Review this authentication flow, find bypass-relevant behavior, tell me what is verified versus inferred, show me the evidence when I ask, remember the important parts across compaction, and do not silently widen scope or manufacture certainty.

The visible interface should remain natural language. Internal structure exists to make strong claims auditable and continuity reliable.

## Non-goals

Awoki is not trying to:

- formalize every thought;
- make RAG authoritative;
- replace source inspection with embeddings;
- turn model observations into machine proof;
- run an autonomous endless reflection/retry loop;
- silently execute target repositories;
- become a generic shell/RPC wrapper;
- provide a complete hosted multi-user IAM/secret-management product;
- hide degraded backends or incomplete verification behind a generic PASS.

## Core mental model

```text
flexible investigation
  user direction
  source observations
  runtime observations
  analyst/model hypotheses
  external/user evidence
       │
       ▼
promote only important things
       │
       ├─ stable evidence/source identities
       ├─ hypothesis / observation / question / gap / note / claim
       ├─ relationships
       ├─ human label + why_saved
       └─ active session work (bounded TODO deliverables + current-session refs)
       │
       ▼
strict boundary where needed
  scope/revision
  provenance/authority
  evidence refs
  contradictions
  verification lifecycle
  bounded correction
       │
       ▼
natural answer / durable continuation
```

Strictness belongs around **identity, provenance, authority, relationships, lifecycle, and claims of verification**. Semantic/analytical content should stay expressive.

## Authority hierarchy / epistemic discipline

Awoki distinguishes at least:

- source/tool evidence;
- runtime observation;
- environment observation;
- user-supplied evidence;
- analyst observation;
- external reference;
- model inference/hypothesis.

A hypothesis can be useful without being machine-proven. A negative search result is not universal proof. A test is evidence of expected/regression behavior, not automatically proof of current production runtime behavior. Static call-graph reachability is possible flow, not proof of runtime execution.

Strong verification result vocabulary:

- `VERIFIED`
- `VERIFIED_WITH_FINDINGS`
- `INCOMPLETE`
- `CONTRADICTED`
- `BLOCKED`
- `NOT_APPLICABLE`

Backend degradation is reported separately from product correctness.

## Repository/source assurance

Important analyses bind to the exact managed source/repository identity.

Representative states:

- `VERIFIED_SNAPSHOT`: exact Git worktree root/commit/view and clean materialized source deeply checked at verification/index time;
- `WORKING_TREE_BOUND`: source usable but whole worktree is not claimed as one immutable snapshot;
- `FILESYSTEM_BOUND`: Git provenance unavailable;
- non-Git content can use canonical content-manifest binding.

Reduced assurance warns; it should not hide readable source.

Git content/index freshness and mutable repository-view metadata are distinct. Harmless view metadata drift must not masquerade as corpus staleness; actual revision/document-set/content/membership drift remains fail-closed where required.

## Retrieval identity

Canonical source/project continuity is primary. Derived retrieval state is rebuildable.

- SQLite FTS: local lexical derived index.
- structural code SQLite: definitions/references/call edges/chunks/branch membership.
- remote embeddings + Qdrant: optional semantic derived index.
- remote reranker: optional ordering/evaluation aid, never authority.

`codebase_search` uses structural/lexical discovery and, when explicitly materialized/current, semantic retrieval plus optional reranking. Search hits are candidates. Behavioral claims require exact source/structure/runtime evidence appropriate to the claim.

Reranker canonical telemetry remains nested under `details.retrieval.reranker`. Captured evidence may expose a bounded derived selector; this must not mutate canonical payloads or trigger additional backend calls.

## Evidence / references

Important rich evidence is content-addressed (`ev_...`) and lives outside compact ledgers. Source candidates use stable `cand_...` identities. Human navigation metadata can add:

- `label`
- `why_saved`
- aliases
- linked refs

Natural-language resolution is navigation only. Close/low-confidence matches return `ambiguous` with no authoritative ID selected.

Candidate provenance distinguishes:

- `first_materialized_in`
- later `observed_in`
- occurrence count

A stable candidate can recur in many evidence captures without changing identity.

Other stable namespaces include verification/checkpoint, relation, TODO/work, continuation, acceptance-run, and acceptance-attempt identities. Do not create durable IDs for every transient thought.

## Compaction / continuity

Conversation text is not canonical state.

The continuity plugin preserves bounded operational/project context and reinjects compact execution invariants plus a **current-session working set** across OpenCode compaction. Multi-step natural-language goals should be projected into the existing bounded OpenCode TODO mirror rather than a new session-intent ledger. Human reference injection is session-scoped: the durable project catalog remains searchable, but old references are not made active by recency alone. Important data remains in durable stores and can be re-fetched by stable ID.

Compaction generation/history is bounded and records trigger when structurally available:

- `automatic_context_pressure`
- `explicit_request`
- `unknown`

Never infer unknown trigger identity from timing.

Generic model-turn anomaly detection is separate from repository readiness/self-resume. Structural anomalies include reasoning-only terminal turns and tool-execution-without-followup. No reasoning text is persisted. Generic recovery does not automatically send endless `continue` messages and does not consume the epistemic corrective budget.

## Detached repository readiness / self-resume

`repository_prepare_*` is the preferred durable parent workflow.

- `mode=local`: structural/FTS readiness only.
- `mode=full`: explicit semantic materialization plus exact vector membership/backend readiness.

Repository readiness is project/repo/source state, not chat state.

Optional continuation is bounded:

- one-shot scheduling;
- lease;
- maximum 48-hour active-chain lifetime, checked again at claim time;
- maximum 3 automatic resume claims per active chain;
- active rescheduling preserves deadline + consumed attempts;
- expired active rescheduling does not grant a fresh lifetime;
- failed/blocked work does not become an unbounded retry loop;
- generic model-turn recovery does not use this detached-job mechanism.

## Self-reflection philosophy

Awoki should not recursively “reflect on reflection.” Reflection is bounded and event-driven.

High-value triggers to evaluate in real work:

1. hypothesis → finding promotion;
2. broad/universal claim;
3. conclusion from absence/negative search evidence;
4. contradiction with earlier conclusion;
5. declaration of verified security property;
6. abandonment of an investigation branch;
7. final security-review conclusion.

A reflection checkpoint should record structural outcomes (claim type, support/refutation, gaps, changed conclusion, need for one bounded follow-up) rather than private chain-of-thought.

## Acceptance/reliability machinery

This is mostly **internal test infrastructure**, not the normal user interface.

`acceptance_run_*` v4 can persist bounded machine-checkable contracts, immutable `aat_...` attempt history, machine-owned prior-attempt conditions, separate execution/orchestration provenance, evidence-scope requirements, tool/invocation limits, and compaction history. A claimed PASS can be downgraded to `incomplete` or `protocol_deviation`.

This machinery exists to test Awoki and other deterministic workflows. It should not leak into ordinary security-review conversation unless the user explicitly asks for formal acceptance/verification.

Acceptance bookkeeping corrections are separate from the reliability epistemic corrective budget.

## OpenCode runtime policy

Fresh builds default to **latest / untested** OpenCode. The build resolves the CLI and aligns the local OpenCode plugin/SDK to that exact version. Running containers do not auto-update.

Operators can select any exact last-known-good version with safe mode. Awoki should fail on real interface incompatibility, not because a static historical version number changed.

Runtime structural metadata records the resolved CLI/plugin/SDK tuple for diagnosis.

## Security/runtime boundaries

- No Docker socket in OpenCode container.
- No privileged/host-network/host-PID deployment in normal topology.
- Source baked into image; only explicit runtime domains writable.
- Remote embedding is explicit materialization intent, not a side effect of opening a project.
- Target-repository Git/search subprocesses strip provider credentials and ambient loader/interpreter/SSH-agent overrides as defense in depth.
- Same-user hostile code is still not safely sandboxed; use a separate credential-free execution environment.
- Deterministic semantics helpers execute only fixed allow-listed helper logic, never repository code.
- Burp side effects retain explicit command boundaries.

## What is almost certainly core

Do not casually remove without realistic replacement evidence:

- managed project/repository/source identity;
- revision/content assurance;
- structural + lexical repository retrieval;
- evidence artifacts and stable evidence/candidate identity;
- distinction between observation/inference/hypothesis/gap/finding;
- provenance/authority;
- contradiction handling;
- bounded verification before strong claims;
- compaction continuity.

## Strong supporting infrastructure — keep but challenge for simplicity

- human labels / `why_saved` / natural-language navigation;
- relations;
- TODO/work continuity;
- bounded corrective action;
- repository readiness parent jobs;
- OpenCode runtime diagnostics;
- automatic-vs-explicit compaction history.

## Internal/experimental complexity that must earn its place

These are useful today but should be evaluated for overlap and UX cost:

- detailed acceptance schemas;
- immutable acceptance attempts and interface ceilings;
- multiple provenance views;
- acceptance-specific evidence selectors;
- detailed runtime anomaly accounting;
- some relation types;
- generic self-resume/continuation UX beyond repository readiness.

Do not add more structure merely because it is possible.

## Current stabilization rule

Before adding a new persistent concept, answer:

1. What concrete failure does it prevent?
2. Can an existing mechanism solve it?
3. Will a normal user see/need it?
4. What state does it persist?
5. How does it age, compact, or get deleted?
6. What happens when it is stale or ambiguous?
7. Can realistic security/code-review work demonstrate value?
8. What could be removed if this is added?

The desired endpoint is **the smallest system that reliably produces excellent long-running evidence-backed investigations**, not the system with the most mechanisms.

## Current evaluation agenda

Feature expansion is frozen except for defects uncovered by realistic work.

Run 10–20 natural security/code-review journeys and measure:

- architecture/retrieval correctness;
- assumption/negative-evidence handling;
- usefulness of hypotheses/findings/gaps;
- reflection triggers that actually change conclusions;
- context/tool cost;
- compaction recovery;
- usefulness versus annoyance of durable references;
- unnecessary MCP calls/persisted objects;
- duplicated mechanisms;
- opportunities to delete/merge code.

See `docs/USEFULNESS_EVALUATION.md`.

## Read order for a new maintainer/context

1. `README.md` — public concept and normal use.
2. This file — identity/invariants/stabilization agenda.
3. `docs/ARCHITECTURE.md` — actual topology/storage/retrieval flow.
4. `AGENTS.md` + `HARNESS.md` — agent execution/evidence rules.
5. `docs/CODE_SEARCH.md` — code retrieval/evidence behavior.
6. `docs/CONTINUITY.md` — work/readiness/compaction state.
7. `docs/RELIABILITY.md` — verification/acceptance machinery.
8. `docs/OPERATOR_REFERENCE.md` — dense operational details and edge cases.

## Release lineage relevant to current identity

Recent releases built the current foundation:

- R9.1.6.6–.8: detached readiness, continuity, stable TODO/evidence integrity.
- R9.1.6.9–.10: bounded self-verification, freshness semantics, verification boundary cleanup.
- R9.1.6.11–.12: OpenCode latest/safe policy, agent runtime diagnostics, compaction-safe acceptance contracts.
- R9.1.6.13: human reference navigation and execution/orchestration provenance separation.
- R9.1.6.14: immutable acceptance attempts, candidate occurrence provenance, ambiguity-safe resolution, compaction trigger classification.
- R9.1.6.15: machine-owned prior-attempt evaluation without self-referential acceptance criteria.
- R9.1.6.16: stabilization/public-documentation release and realistic-journey evaluation program.
- **R9.1.6.17: first J1-driven simplification release—normal exact-search ergonomics, TODO-backed active goal continuity, current-session reference injection, explicit source-window continuation, scoped freshness wording, and removal of empty project-rule boilerplate.**
- **R9.1.6.18: J2-driven stabilization—first-class structured ripgrep exact search without Bash, plus a slim `project_open` orientation projection that keeps dense continuity behind explicit tools.**
- **R9.1.6.19: final internal pre-semver operator-onboarding release—J3 classified both R9.1.6.18 changes KEEP; public/operator docs made Docker -> SSH -> tmux -> OpenCode, detach/reconnect, and persistence boundaries explicit without adding analysis machinery.**
- **v0.1.0: first public semantic-versioned release of the R9.1.6.19 stabilization baseline, including the macOS/Docker Desktop SSH-bootstrap portability fix that replaces the single-file `authorized_keys` bind with validated public-key injection.**
- **v0.1.1: patch release fixing structural call-reference occurrence identity for chained Tree-sitter calls; extraction profile v3 forces stale structural indexes to rebuild.**
- **v0.1.2: patch release adding host + Docker-view Qdrant bind-storage preflight so collection-directory failures are repaired or rejected before vector work starts.**
- **v0.1.3: release-integrity correction preserving the published v0.1.2 tag while establishing a correctly targeted release containing the Qdrant preflight and existing structural extraction v3 fix.**
- **v0.1.4: clean Tree-sitter extraction baseline fixing type-owned method classification and stable branch control-context labels; extraction profile v4 invalidates stale v3 structural indexes.**
- **v0.1.5: Qdrant runtime-storage integrity patch validating the actual long-lived Qdrant container, rejecting stale checkout bindings, and gating OpenCode startup on a successful live collection-directory write probe; extraction remains v4.**
- **v0.1.6: Qdrant readiness-path correction probing `qdrant:6333` from the same internal Docker network consumed by OpenCode, while retaining host-mode readiness as a generic fallback; validator and runtime regressions follow the delegated Qdrant-first launcher contract and extraction remains v4.**
- **v0.1.7: Qdrant non-interactive readiness correction adding Compose `-T` to the stdin-fed Docker-network probe, with regression and validator coverage preventing TTY allocation from breaking automated startup; extraction remains v4.**

When a future context proposes another feature, compare it against this identity before implementing it.
