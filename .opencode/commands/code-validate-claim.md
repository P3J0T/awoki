---
description: Deterministically verify code or source-logic requests, decomposing broad questions into atomic proof obligations
---

Use the `project-continuity` skill. Treat `$ARGUMENTS` as a user-facing
verification request; it does not have to already match the strict MCP claim
grammar.

First establish one current structural source snapshot. Call `code_index_status`.
If `freshness.lexical_current` is false (or no structural index exists), call
`project_refresh(include_code=true, include_qdrant=false)` once. A stale code
vector alone does not require refresh because Qdrant is not verification evidence.
After that, keep `refresh_index=false` on individual proof calls so a multi-claim
verification does not repeatedly rebuild the repository.

If the requested proof depends on a supported Go language/stdlib primitive rather
than repository control-flow structure, call `code_semantics_check` and use the
observed result directly. This is mandatory for supported cases such as
`path.Join`/`path.Clean`, `time.ParseDuration` or duration multiplication, failed
`error` type assertions, `strings.Replace`, `url.Parse`, and `reverse_proxy_rewrite_headers` for `httputil.ReverseProxy` forwarded-header pre-processing. Do not substitute
mental arithmetic or remembered Go behavior. The checker executes generated
allow-listed stdlib code only, never repository code, and reports toolchain
alignment with the attached project's plain-text `go.mod` when available.

If `$ARGUMENTS` already expresses one supported atomic obligation, call
`code_validate_claim` directly. Supported atomic forms include exact direct
calls, bounded reachability, definition location, and supported exact
condition/outcome claims.

For a broad request such as "validate the OAAF tree execution", "verify how this
input file is processed", or "confirm every yes/no branch":

1. Use `codebase_search` only to discover candidate entry points, files, and
   symbols. Semantic/FTS results are navigation, never proof.
2. Resolve the relevant symbols with `code_definition`, `code_callers`,
   `code_callees`, and `code_path` as needed. For a multi-step execution request,
   use `code_flow_graph` from the exact entry point to scope the relevant reachable
   graph, then inspect the necessary current source with `code_source_window`.
   Important source windows carry `evidence_id`; call `code_evidence_verify`
   before reusing old evidence after source or Git-view changes.
   Traverse only resolved graph edges and keep ambiguous/unresolved boundaries.
3. Reconstruct the requested logic into a bounded set of small, explicit proof
   obligations. For decision logic, preserve the exact condition, branch, and
   terminal outcome rather than paraphrasing them into synonyms.
4. Call the strict `code_validate_claim` MCP primitive separately for every
   obligation it supports. Do not pass the original vague request straight to
   the strict primitive merely to obtain `INCONCLUSIVE`.
5. Report each obligation as `VERIFIED`, `REFUTED`, `INCONCLUSIVE`,
   `STALE_SOURCE`, or `AMBIGUOUS_SYMBOL`. The broad request is fully verified
   only when every required obligation is verified. One refuted obligation
   refutes the corresponding broad assertion; any unresolved required boundary
   keeps the overall result inconclusive.

The strict proof primitive may use exact symbol lookup, current source hashes, a
fresh source reparse, lexical-scope checks, exact AST conditions/outcomes, and
edge-by-edge validation of candidate graph paths. Graph resolution alone is not
proof. Embeddings, Qdrant similarity, reranking, and semantic guesses must never
be used as verification evidence.

When the implementation uses unsupported dynamic behavior or a language for
which the strict proof primitive cannot prove the required obligation, show the
observed source separately but keep the deterministic verdict `INCONCLUSIVE`.
