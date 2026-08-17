---
description: Investigate the active project repositories from natural language with evidence-backed structural analysis by default
---

Use the `project-continuity` skill. Treat `$ARGUMENTS` as a repository
investigation request, not merely a search string. The user does not need to say
"deterministically" for ordinary code-understanding work.

Start with `codebase_search(query=$ARGUMENTS, mode="auto")`. Broad discovery spans
all enabled registered repositories. When a later exact source/graph/evidence/
semantics/exhaustive operation is ambiguous, pass the repository identity returned
by discovery instead of guessing a child checkout. Infer result view from the request:

- quick locations/pointers only -> `peek`
- normal explanation/investigation -> `context`
- complete relevant symbol bodies -> `full`

Semantic/FTS/Qdrant hits are discovery only. Do not present behavioral conclusions
from retrieval similarity alone. Hits carry a `source_role` hint. For ordinary behavior
questions, let `result_focus=auto` prefer query-relevant production implementation
while keeping tests/config/schema/docs available as corroboration/discovery. If
the request explicitly asks for tests, regressions, fixtures, config, or edge
cases, honor that focus rather than forcing production-first output. Bounded
structural promotion may add production candidates through verified graph edges,
but graph connectivity is not behavioral relevance: a promoted candidate must
still earn relevance against the original query and never becomes production
proof merely because it is connected.
When the user asks to compare retrieval backends, use the real MCP diagnostics
(`mode=lexical`, `use_fts`, `use_qdrant`, `use_reranker`,
`structural_promotion`, explicit `result_focus`, and `strict_backends`) rather
than simulating a backend or silently substituting another mode. Read explicit
reranker requested/attempted/applied telemetry; never infer execution from score
shape.
For security-boundary/edge-condition questions, run a follow-up discovery focused on related
`test`/`test_fixture` evidence after identifying the production symbol, then reconcile the tested
contract with hash-checked production source.

For ordinary repository questions, continue from discovery into exact evidence:

1. Resolve the strongest candidate symbols with `code_definition` and use
   `code_callers`, `code_callees`, or `code_path` when those relationships matter.
2. If the request is flow-oriented (for example: explain/trace processing,
   request/file/input lifecycle, decision tree, pipeline, branch execution),
   identify one exact entry point and call `code_flow_graph` with a bounded depth.
   Expand depth only when needed; do not dump the entire repository graph.
3. Inspect the relevant current source with `code_source_window`. Trace exact
   branch predicates, assignments/aliases, argument passing, transformations,
   returns, and terminal outcomes. Long source lines must remain explicitly
   bounded/truncated. Preserve the returned `evidence_id` for important source
   windows and use `code_evidence_verify` before reusing them after edits,
   branch/view changes, or a long investigation. The evidence ID detects drift;
   it is not a signature or authorship attestation.
4. Use the strict `code_validate_claim` primitive selectively for important
   bounded propositions it supports. Do not force the whole investigation into a
   single atomic claim.
5. If `code_source_window` returns `deterministic_semantics.recommended=true`,
   run the listed operation(s) before making the corresponding concrete claim.
   More generally, if a conclusion depends on a supported deterministic Go primitive, call
   `code_semantics_check` rather than reasoning from memory. Supported operations
   include `path.Join`, `path.Clean`, `time.ParseDuration`/duration multiplication,
   failed `error` type assertion, `strings.Replace`, `url.Parse`, and `reverse_proxy_rewrite_headers` for `httputil.ReverseProxy` forwarded-header pre-processing. Repository
   code is never executed by this check. Respect `toolchain_alignment`; a local
   stdlib observation is not target-runtime proof when Go major/minor differs.
6. Distinguish evidence strength in the answer:
   - `VERIFIED`: strict atomic proof succeeded;
   - `SOURCE-CONFIRMED`: directly supported by hash-checked current source and
     exact structural relationships but outside the strict proof grammar;
   - `AMBIGUOUS` / `INCONCLUSIVE`: dynamic, unresolved, unsupported, or multiple
     legitimate targets;
   - `DISCOVERY ONLY`: semantic/lexical candidate not yet source-confirmed.

A static call/flow graph describes possible source-level flow; it is not proof a
runtime execution occurred. Do not silently turn a static path into an observed
runtime claim.

Do not start broad repository discovery with OpenCode `Grep` or unrestricted
`rg --json` when an Awoki structural index is available. Make at least one real
Awoki indexed/structural discovery attempt before declaring broad fallback. If indexed discovery
fails or is materially insufficient, try OpenCode's native `Grep` first. When
native Grep returns a complete usable result, use that candidate universe. If it
errors, truncates, hits a giant-line/client transport limit, or leaves coverage
uncertain, call `code_text_search` instead of attempting larger raw JSON output.
`code_text_search` is exhaustive over the declared textual repository scope, materializes each
query/snapshot once, and uses the cursor both to resume discovery after the MCP
operation budget and to page cached results. Continue with `next_cursor` until
`scan_complete=true`, then `search_complete=true`, and require
`repository_universe_complete=true`. For an explicit forensic search that must include Git-ignored untracked files, set `include_ignored=true`; sensitive files such as `.env` remain opaque in returned previews. Do not pipe fallback
searches through `head`. The `.harness/bin/code-search-fallback PATTERN PATH
[PATH ...]` helper is only the MCP-unavailable diagnostic equivalent. Treat every
lexical fallback result as `DISCOVERY ONLY` and return to indexed, hash-checked
source before asserting behavior. Policy-excluded/unindexed evidence remains
`INCONCLUSIVE`.

Report the active branch/commit and any stale/degraded/ambiguous boundaries that
materially affect the answer. Treat `VERIFIED_SNAPSHOT`, `WORKING_TREE_BOUND`,
and `FILESYSTEM_BOUND` as source-provenance assurance levels, not confidence in
human authorship. Git author/committer names are metadata claims. Avoid narrating every internal tool call unless the
user asks for the audit trail.
