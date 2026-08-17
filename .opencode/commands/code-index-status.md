---
description: Inspect the active project's passive structural code-index state
---

Use the `project-continuity` skill. Call `code_index_status` first. It is a
passive/local status read: it must not rescan/hash the repository or contact
Qdrant. Report active branch, commit, dirty state, parser runtime,
structural/fallback counts, graph counts, reference-integrity diagnostics,
recorded SQLite/vector state, and whether source freshness can reuse the same
materialized Git-view fingerprint or requires explicit/local refresh/deep
verification. Snapshot provenance and index freshness are separate: a stable
sparse/submodule/replacement-ref view can remain reusable while still reporting
`WORKING_TREE_BOUND`. A merely clean-looking Git status is not sufficient after
status-suppressing state such as assume-unchanged/manual skip-worktree or weakened
Git stat trust.

If the user asks to prove byte-level repository freshness or live code-Qdrant
reachability, call `code_index_verify` explicitly. That operation is allowed to
perform the full policy/hash source scan and, when requested, a bounded live
Qdrant collection check.

Report repository assurance (`VERIFIED_SNAPSHOT`, `WORKING_TREE_BOUND`, or `FILESYSTEM_BOUND`) and view anomalies when present. Keep this passive; use `code_index_verify` only when the user asks for deep verification.
