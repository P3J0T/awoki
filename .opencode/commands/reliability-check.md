---
description: Run a rigorous local reliability gate without pushing, creating a PR, or contacting CI
---

Load the `reliability-verification` skill and run its Reliability workflow for: $ARGUMENTS

First define the exact claim being validated and enumerate concrete required checks. Call `reliability_start(mode="reliability")`, record each actually observed result with `reliability_record_check`, and record load-bearing factual conclusions as structured claims when useful. Use `reliability_verify_code_claim` / `reliability_verify_semantics_claim` when those deterministic verifiers apply; otherwise keep unsupported conclusions explicitly inconclusive rather than self-certifying them. For interpretive/load-bearing reasoning that is broader than an atomic claim, record concise assessment nodes with `reliability_record_assessment`, run `reliability_verification_checkpoint`, and allow at most one safe corrective evidence action before a final checkpoint. Then call `reliability_finish`. Adapt the gate to code, reverse engineering, research, or an unfinished pause. Never push, create a PR, publish, or contact CI. A failed or missing required check cannot be finalized as `passed`; a refuted or contradictory verified structured claim fails the run.
