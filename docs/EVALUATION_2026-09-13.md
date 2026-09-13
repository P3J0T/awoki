# Supervised workflow evaluation — 2026-09-13

This development snapshot includes compact retrieval/instruction delivery,
source-bound memory and follow-ups, compaction recovery, and a focused findings
review using existing reliability records. It is **not an autonomous-truthfulness
or release acceptance pass**.

Mechanical checks on the frozen implementation:

- 724 tests on Linux (1 skip) and macOS (13 environment-dependent skips).
- OpenCode plugin compilation and session-hook checks against SDK 1.18.30 on Linux.
- A real verifier-adapter probe checked a Python direct call and refuted an
  incorrect definition path. Arbitrary analyst wording inherited neither verdict.
- Source/fixture integrity and staged whitespace checks passed. Existing
  subprocess ResourceWarnings remain.

One fresh Qwen `qwen3.8-27b-turbo` trial used new synthetic authorization cases,
working embeddings/Qdrant/reranking, real explicit compaction, and fresh-session
recall. No target code was executed. Quantization was not independently measured.

The model distinguished an independently guarded plugin from an unguarded one,
used the review labels, and later rejected misleading blanket claims. However:

- It treated “not present in the structural index” as “file absent”, then saved
  and inherited that false README claim.
- It selected an unsupported verifier proposition instead of a supported narrow
  claim. The gate correctly remained blocked.
- It saved a separate note without the report's qualifications; later follow-ups
  retained only what their selected parent actually contained.
- Recall did not consistently reopen the current review and source/docs.

The full semantic/workflow acceptance therefore **did not pass**, despite successful
runtime completion and regression checks. No model-quality retries were used.
The trial took 311.31 seconds and 35 tool calls (including 6 rejected status writes).
Different fixtures from earlier evaluations prevent a matched improvement claim.

Use `/verify these findings` as a supervised review aid. Exact checked scopes,
evidence attachment, source freshness and semantic truth remain different things.
An index miss cannot prove file absence; a report link cannot certify a paraphrase.
See [the reliability model](RELIABILITY.md) for the implemented boundaries.
