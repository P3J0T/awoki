---
description: Prepare or execute an explicitly authorized delivery gate, optionally using no-mistakes
---

Load the `reliability-verification` skill and run its Ship workflow for: $ARGUMENTS

Start the delivery ledger with `reliability_start(mode="ship")`, enumerate required checks, and record their observed results with `reliability_record_check`. Record every load-bearing required factual claim and use `reliability_verify_code_claim` / `reliability_verify_semantics_claim` wherever supported; ship mode must not pass with missing claims, unsupported required claims, stale evidence, refuted claims, contradictions, or model-self-certified verification. If required assessment nodes are used, run `reliability_verification_checkpoint` and require a current clear checkpoint; at most one bounded corrective verification action is allowed. Call `reliability_finish` only after the deterministic check, claim, and required assessment gates are satisfied. Inspect the actual Git topology. Use no-mistakes only when installed, configured, and appropriate. Ask for explicit authorization before push, pull request, CI, publish, or release actions. Do not invent a remote or PR workflow for local-only repositories.
