---
description: Inspect passive retrieval/code-index state, with explicit opt-in live probes
---

Call `retrieval_status` directly first. It is a passive local status read and must
not contact Qdrant, the embedding endpoint, or the reranker. Report:

- embedding provider, model label, actual deployment identity, endpoint,
  auth-present boolean, normalisation, and explicit vector size;
- Qdrant URL, general collection, dedicated code collection, local client-library
  availability, and last-known live probe state (which may be `not_probed`);
- reranker enabled/provider/endpoint, whether TEI selects the model server-side,
  auth-present boolean, and fallback mode;
- last embedding/reranker errors and degraded state.

If live backend health is actually required, call `retrieval_probe` explicitly.
By default it probes Qdrant only. Set `probe_embedding=true` and/or
`probe_reranker=true` only when those network checks are needed. The probe uses one bounded operation-level timeout budget shared across the
requested checks and fixed synthetic health-check text; it never sends project,
source, memory, or user text.

When a project is attached and code-index state is relevant, call
`code_index_status`. It is also passive: it must not rescan/hash the repository
or contact Qdrant. Report:

- active branch, commit, dirty state, and whether source freshness is proven by
  clean Git commit identity or requires explicit verification;
- parser provider/version and structural versus fallback file counts;
- dedicated code SQLite and call-graph counts;
- recorded code-Qdrant collection/membership state;
- exact lexical availability when vector retrieval is degraded.

If byte-level source freshness or live code-Qdrant reachability must be proven,
call `code_index_verify` explicitly. That operation is intentionally expensive:
it performs the full policy/hash source scan and optionally a bounded live
code-Qdrant collection check.

Never print API keys. If effective runtime values differ from `.env`, explain
that `.env` changes require service recreation and a new OpenCode process. Do not
try to repair an already-running MCP through a Bash `export`.
