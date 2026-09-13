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

### Focused findings review

“Review/verify these findings before I act” (or `/verify these findings`) means a bounded
review, not a new memory system or a gate on every note:

1. Reuse a relevant run or `reliability_start(mode="verify")` with one concrete
   required check: review the requested findings and their boundaries. Split
   load-bearing conclusions into narrow claims/observations plus explicit gaps.
   Check both alternatives: a delegated component may enforce its own checks,
   or may actually lack them. Open the component/config when available; otherwise
   record the missing evidence, not a guess. Compare docs to code only after
   reading both; do not assume docs are wrong because another case had old docs.
2. Reopen source and contradictory evidence. Use `reliability_verify_code_claim`
   or `reliability_verify_semantics_claim` only for their supported exact scope.
   Keep richer source interpretations as evidence-linked assessments, and use
   gap/question nodes for unresolved behavior. Never replace the requested claim
   with an easier structural fact and present that as the original claim proved.
3. Record the observed review check, run the existing verification checkpoint,
   and read `reliability_status(view="review")`; follow `next_call` until complete.
   Use its labels: MACHINE_CHECKED/MACHINE_REFUTED apply only to `checked_scope`
   at capture time; EVIDENCE_ATTACHED_UNVERIFIED is interpretation, not proof;
   UNKNOWN/STALE/CONTRADICTED remain visible. An assessment checkpoint's raw
   VERIFIED means graph/evidence requirements passed, not semantic verification.
4. If needed, use at most the run's existing corrective budget for one relevant
   correction, then checkpoint once more. Finalize as reliably-paused when required
   unknowns remain. `reliability_finish` saves the ordinary report artifact with
   qualifications. For later notes/checkpoints, preserve that report source or
   use `based_on` with its continuity record; new wording inherits no verdict.
5. On recall/compaction, reopen the linked review and relevant source before
   relying on a conclusion. The report may have changed; its review hash describes
   the current recorded items, not a certificate for an earlier paraphrase.

This reviews what was recorded, not every possible omission. A small model can
still miss an important unknown or misread code; consequential decisions may
need independent review. Do not add ceremonial checks to casual exploration.

### Full reliability run

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
12. Produce a reliability report when the result is durable, then finalize with `reliability_finish`; required assessment state must have a current passing checkpoint while failed/missing checks and required missing/inconclusive/stale/refuted/contradictory claims remain fail-closed. Honor `reporting_boundary`: disclose inconclusive/error/unperformed verification in the final answer. Source freshness and a check-only pass do not prove behavioral findings.
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
