---
name: reliability-verification
description: Verify important claims, run an adaptive local reliability gate, or prepare explicit shipping work without forcing exploratory projects into a rigid delivery workflow.
compatibility: opencode
metadata:
  scope: project
  workflow: verify-reliability-ship
---

# Reliability Verification

## Choose the requested level

- Explore: preserve freedom and uncertainty; do not force a gate.
- Verify: check the load-bearing claims with the smallest adequate evidence pass.
- Reliability: prove the current completion or pause claim locally.
- Ship: perform reliability first, then use delivery tooling only with explicit authorization.

Read `docs/RELIABILITY.md` before a reliability or ship run.

## Verify workflow

1. State the important claims being checked.
2. Reopen the actual source, configuration, log, artifact, or live tool state.
3. Search project continuity for related, corrected, or contradictory records.
4. Distinguish observation, inference, and hypothesis.
5. Run focused checks when available.
6. Report what remains unverified.

For a Burp claim, load `burp-workflow` and use direct Burp MCP for live state. Do not change the Burp workflow or substitute another HTTP client for Burp state.

## Reliability workflow

1. Define the exact claim: completed feature, validated analysis, or reliably paused investigation.
2. Determine the work type and adapt the gate.
3. Enumerate required checks before running them, then create a durable ledger with `reliability_start(mode="reliability")`.
4. Execute checks and record every observed result with `reliability_record_check`.
5. For load-bearing factual conclusions, record structured atomic claims. Use `reliability_verify_code_claim` when strict source/graph proof applies and `reliability_verify_semantics_claim` for supported runtime primitives. Use `reliability_record_claim` for an unsupported/inferred boundary; never self-certify it as verified.
6. At run start, declare the subject contract when it matters: `required_claims`, `required_properties`, and a bounded `corrective_budget` (normally 1). Each required property is materialized as a required check and therefore needs observed evidence before finalization. No required claims means the machine claim gate is `NOT_APPLICABLE`, not success.
7. When interpretation is richer than an atomic verifier claim, use `reliability_record_assessment` for concise claims, hypotheses, observations, questions, contradictions, gaps, decisions, or non-gating notes. Keep semantics expressive. Put rich output in `ev_...`; structure only identity, authority, evidence refs, requirements, and lifecycle. Create canonical edges independently with `reliability_record_relation` after the relevant nodes exist.
8. Run `reliability_verification_checkpoint` before claiming completion when required assessment nodes exist. Results are `VERIFIED`, `VERIFIED_WITH_FINDINGS`, `INCOMPLETE`, `CONTRADICTED`, `BLOCKED`, or `NOT_APPLICABLE`. Backend degradation is always surfaced but blocks only an assessment that explicitly requires that capability, for example `requirements=["reranker_complete"]`; cross-source evidence is allowed unless `single_evidence_scope` is explicitly required.
9. If the checkpoint needs a corrective action, perform at most **one** safe high-value correction: call `reliability_consume_corrective_budget` before performing it, record the new evidence/assessment state, then run one final checkpoint. Checkpoints do not consume budget. Do not recursively reflect, repeat the same failing check, mutate source/configuration to make a claim pass, widen project scope, or restart failed backends without authorization.
10. Review the deterministic claim gate plus the assessment checkpoint, privacy, indexing, source, correction, and uncertainty boundaries. `VERIFIED_WITH_FINDINGS` still surfaces contradictions/gaps/findings; a passing checkpoint does **not** turn model inference into machine proof.
11. When composing a reliability run with an acceptance run, use `reliability_aggregate_verdict` so component verdicts and the overall result remain explicit.
12. Produce a reliability report when the result is durable, then finalize with `reliability_finish`; required assessment state must have a current passing checkpoint while failed/missing checks and refuted/contradictory verified claims remain fail-closed.
13. Do not push, create a PR, call CI, or publish.

### Code gate

Inspect the actual diff, detect unrelated changes, discover repository-native validation commands, run relevant tests/lint/build/type checks, review error/security implications, and report untested paths.

### Reverse-engineering or research gate

Enumerate important conclusions, require evidence references, search for contradiction, separate fact from inference, verify reproducibility, ensure raw evidence/secrets did not enter broad indexes, and refresh continuity. Use the assessment graph as a **structured spine, not a semantic straitjacket**: natural-language hypotheses, analyst observations, alternative explanations, negative-evidence gaps, and short reasoning summaries are allowed. The strict part is provenance/authority/relationships and explicit evidence references.

### Unstructured work gate

Ask what is being claimed, what supports it, what remains unknown, what should not be assumed, and whether another session can resume accurately. Do not manufacture software-delivery requirements.

## Ship workflow

1. Complete or review the local reliability evidence, then start the delivery ledger with `reliability_start(mode="ship")`.
2. Record all required checks and every load-bearing required claim. Ship mode requires at least one structured claim and machine-verifier receipts for required claims; inconclusive/stale claims block, refuted/conflicting claims fail.
3. Inspect Git topology and available tooling.
4. Use no-mistakes only when installed, configured, and suitable.
5. Finalize the local ship gate with `reliability_finish`.
6. Obtain explicit authorization before any push, PR, CI, publish, or release action.
7. If no remote exists, preserve a local reliability result rather than inventing a delivery target.
