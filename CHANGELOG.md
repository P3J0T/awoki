# Changelog

Public releases use semantic versioning. The historical `R9.1.6.x` entries below are the internal pre-v0.1.0 development/stabilization line.

## v0.1.6 — Qdrant Docker-network readiness correction

- Fixes OpenCode-over-SSH startup waiting on host `127.0.0.1:6333` even though the runtime consumes Qdrant through the internal Docker service `qdrant:6333`.
- The SSH launcher now verifies readiness from a temporary `awoki-opencode-ssh` probe container on the same Compose network before the long-lived OpenCode container starts.
- Retains generic host-loopback readiness for host-mode callers and explicit operator overrides.
- Keeps the v0.1.5 live Qdrant storage identity/write probe and fail-closed startup ordering.
- Updates validation and runtime-contract regressions to the delegated Qdrant-first launcher architecture.
- Retains `awoki-symbol-extraction-v4`; runtime readiness changes do not alter structural extraction semantics.
- Public package version is `0.1.6`; internal harness version is `10.20`.

## v0.1.5 — Qdrant running-storage integrity

- Validates the actual long-lived Qdrant service container instead of relying only on a disposable Compose probe.
- Rejects stale Qdrant containers created from another Awoki checkout before semantic work starts.
- Starts Qdrant first, waits for readiness, performs a live `/qdrant/storage/collections` write probe, and only then starts the OpenCode SSH runtime.
- Routes OpenCode runtime recreation through the same Qdrant-first verified startup path.
- Retains `awoki-symbol-extraction-v4`; this patch changes runtime storage validation, not structural extraction semantics.
- Public package version is `0.1.5`; internal harness version is `10.19`.


## v0.1.4 — clean Tree-sitter extraction baseline

- Preserves the chained-call reference-identity correction and Qdrant bind-storage startup preflight from the verified v0.1.3 source.
- Fixes Tree-sitter Python class methods being emitted as `function` instead of `method` when the grammar uses a generic `function_definition` node.
- Normalizes Tree-sitter branch control-context labels such as `if_statement` to the stable language-level label `if`.
- Bumps the extraction profile to `awoki-symbol-extraction-v4` so indexes produced with the corrected symbol/control-context semantics rebuild instead of being reused.
- Public package version is `0.1.4`; internal harness version is `10.18`.

## v0.1.3 — release-integrity correction

- Corrects the public release lineage after `v0.1.2` was accidentally tagged at the preceding structural-reference-fix commit before the Qdrant preflight commit was created.
- Preserves the published `v0.1.2` tag rather than rewriting public history; `v0.1.3` is the first correctly targeted release containing the Qdrant bind-storage startup preflight.
- Retains the v0.1.1 structural call-reference identity fix and `awoki-symbol-extraction-v3`; extraction semantics are unchanged, so no unnecessary profile bump or structural-index rebuild is introduced.
- Public package version is `0.1.3`; internal harness version is `10.17`.

## v0.1.2 — Qdrant bind-storage startup preflight

- Fixes a Docker Desktop/macOS failure where Qdrant is healthy but collection creation returns HTTP 500 because `/qdrant/storage/collections` is absent or not writable from Docker's bind-mount view.
- Adds `.harness/bin/prepare-qdrant-storage`: initialization verifies the host path, while every Qdrant startup materializes and write-probes the exact mounted path using the configured Qdrant image before the service starts.
- `make docker-up` and the OpenCode-over-SSH launcher now fail early on an unusable Qdrant bind instead of discovering the problem after structural indexing and embedding work.
- Keeps the existing `data/qdrant` bind and full-backup format unchanged; no migration to a Docker named volume is required.
- Public package version is `0.1.2`; internal harness version is `10.16`.

## v0.1.1 — structural call-reference identity patch

- Fixes Tree-sitter structural indexing for nested/chained calls whose outer invocation nodes share the same start position, observed in Java method chains such as `Optional.of(...).map(...).map(...)`.
- Call-reference identity is now anchored to the exact callee/name token when the grammar exposes one; the larger invocation node remains the source/context span.
- Keeps the fail-closed reference-identity conflict check intact; the parser is corrected instead of weakening store integrity.
- Bumps the structural extraction profile to `awoki-symbol-extraction-v3`, forcing stale v2 structural indexes to be rebuilt before they are treated as current.
- Public package version is `0.1.1`; internal harness version is `10.15`.

## v0.1.0 — first public semantic release

- Includes the macOS/Docker Desktop SSH-bootstrap portability fix: no single-file `.ssh-container/authorized_keys` host bind; only the validated public key is injected and installed inside the container, while the private key remains host-only.
- Direct raw Compose startup without the Awoki launcher fails closed if the SSH public-key bootstrap value is absent.

This is the first public semantic-versioned Awoki release. It packages the stabilized R9.1.6.19 baseline without adding a new analysis mechanism solely for the version transition.

- Public release version reset to `v0.1.0`; Python package metadata is `0.1.0`.
- Keeps the J1/J2/J3-backed stabilization decisions, operator onboarding, tmux/OpenCode workflow, structured `code_exact_search`, slim `project_open`, compaction continuity, evidence/provenance, and bounded verification behavior from the internal line.
- Internal harness protocol/version is `10.14`; public release numbering and harness protocol numbering are intentionally independent.

## R9.1.6.19 — operator onboarding and J3 validation

This release adds no new analysis mechanism. J3 validated that the two R9.1.6.18 additions pay their complexity cost in realistic work: structured `code_exact_search` answered a real production-only cross-file enumeration in one complete call, while slim `project_open` removed the duplicated continuity dump without losing the prior-review pointers that were actually reused.

- Made the recommended day-to-day runtime path explicit in the public and operator docs: host Docker runtime -> SSH -> `tmux new -A -s awoki` -> OpenCode.
- Documented safe detach/reconnect behavior, the difference between SSH disconnect and container recreation, and a practical tmux window layout for OpenCode/shell/tests/logs.
- Clarified that tmux is recommended operational process continuity, not a prerequisite for Awoki correctness and not a replacement for durable Awoki project/compaction state.
- Recorded the J3 KEEP result for both `code_exact_search` and slim `project_open`; no change to `project_search` refresh semantics or `codebase_search` payload sizing.
- Bumped harness identity to 10.13 and Python project metadata to 9.1.6.19.

## R9.1.6.18 — J2 exact-search and project-open stabilization

This release implements only the two J2 changes selected for action; `project_search` refresh semantics and `codebase_search` output size are intentionally unchanged.

- Added first-class `code_exact_search` MCP backed by ripgrep, with typed patterns, match/file/count modes, bounded context, include/exclude globs, hidden/ignored-file policy, credential-stripped repository subprocesses, sensitive-match redaction, bounded pagination metadata, and no Bash/raw-CLI passthrough.
- Slimmed normal `project_open` output to repository/readiness state, current session TODO/reference work, recent prior-material pointers, and bounded continuation guidance. Dense SITUATION/HANDOFF/reflection/important-knowledge projections remain available through explicit `project_resume`, `project_handoff`, and `project_search`.
- Updated the public/maintainer diagrams and exact-search guidance so normal source navigation is: Awoki conceptual retrieval -> OpenCode Grep for simple exact lookup -> `code_exact_search` for structured ripgrep power -> `code_text_search` only for its stronger materialized exhaustive-coverage/transport contract.
- Added regressions for slim project opening and structured exact-search modes/continuation/scoping.
- Bumped harness identity to 10.12 and Python project metadata to 9.1.6.18.

## R9.1.6.17 — J1 stabilization friction fixes

This release implements only changes justified by the first realistic security-review journey.

- Clarified the normal source-navigation policy: Awoki indexed search for conceptual discovery, OpenCode Grep for ordinary exact lookup, native `rg` for complex/exhaustive exact enumeration, and `code_text_search` when deterministic exhaustive coverage or transport recovery is required. Acceptance contracts can still forbid native tools.
- Reused the existing native TODO mirror as the bounded active working set for multi-step user goals/deliverables across compaction instead of adding a separate session-intent ledger.
- Scoped compaction reference injection to references actually used by the current session (plus active acceptance references); older durable project references stay searchable but are no longer injected by recency alone.
- Added explicit `code_source_window.truncation` metadata with reasons, completion state, clipped-line list, and continuation guidance.
- Renamed generated continuity freshness presentation to `project_memory_index_current` and explicitly distinguished it from structural code-index freshness while retaining the legacy `fresh` compatibility field in machine responses.
- Stopped creating the generic two-line project-local `AGENTS.md`; exact legacy boilerplate is removed on layout maintenance, while user/project-authored rules are preserved.
- Recorded J1's keep/simplify conclusions in the usefulness-evaluation document.
- Added Mermaid diagrams for the normal investigation flow, component/data-store boundaries, and automatic-compaction continuation path so the public and maintainer docs visualize execution rather than only describe it.
- Bumped harness identity to 10.11 and Python project metadata to 9.1.6.17.

## R9.1.6.16 — Stabilization and public documentation

This release intentionally adds no new agent capability.

- Replaced the former 1,000-line front-page README with a public-facing conceptual/install/use guide.
- Preserved the previous dense operational material as `docs/OPERATOR_REFERENCE.md`.
- Added `docs/AWOKI_IDENTITY.md`, a dense maintainer/future-context identity and invariant map.
- Added `docs/USEFULNESS_EVALUATION.md`, the real security/code-review journey plan for deciding what to keep, simplify, merge, or remove.
- Updated installation guidance for the current Docker/OpenCode latest-or-safe runtime policy.
- Split documentation validation: README is checked for onboarding/mental-model guidance; the operator reference remains checked for detailed retrieval/index contracts.
- Bumped harness identity to 10.10 and Python project metadata to 9.1.6.16.

Development policy after this release: discretionary feature expansion is frozen until realistic investigation journeys demonstrate missing capability. Green internal tests alone are not sufficient evidence that a mechanism should exist.

## R9.1.6.15 — Non-self-referential acceptance correction

- Added machine-owned prior-attempt requirements to acceptance v4.
- A later bookkeeping correction can machine-pass using immutable earlier `aat_...` outcomes without predicting its own future result.

## R9.1.6.14 — Acceptance history and compaction provenance

- Immutable bounded acceptance-attempt history.
- Per-interface invocation ceilings.
- Candidate `first_materialized_in` / `observed_in` provenance.
- Ambiguity-safe natural-language reference resolution.
- Structural automatic-vs-explicit compaction trigger classification.

## R9.1.6.13 — Reference and provenance boundaries

- Human-readable durable references (`label`, `why_saved`, aliases) while stable IDs remain authoritative.
- Separate execution and acceptance-orchestration provenance.
- Canonical Awoki MCP tool identity.

## R9.1.6.12 — Compaction-safe acceptance boundaries

- Durable test contracts and machine enforcement across compaction.
- Broader agent-runtime terminal-turn anomaly detection.
- Bounded Awoki self-check MCP groups.

## R9.1.6.11 — OpenCode runtime policy and bounded runtime diagnostics

- Fresh builds can resolve latest OpenCode; operator-selected safe mode can pin an exact last-known-good version.
- Running containers remain immutable/no silent auto-update.
- Structural agent-turn diagnostics and acceptance progression support.

## R9.1.6.9–R9.1.6.10 — Verification/freshness hardening

- Bounded self-verification model.
- Non-vacuous required-claim handling.
- First-class reasoning relations and correction-budget ownership.
- Git content freshness separated from harmless repository-view metadata drift.

Earlier release history remains available in Git.
