# Awoki Structural Code Search — Implementation Plan

**Planning baseline:** Awoki commit `98f5431`  
**Final architecture:** one native Awoki structural code-search engine; no external code-index service and no runtime legacy/structural switch.  
**Scope:** Native Awoki implementation only. No `open-codebase-index` dependency, process, storage format, or runtime integration.

## 1. Objective

Upgrade Awoki's repository search from fixed-size text chunks into a native structural code-search subsystem with:

- Tree-sitter symbol-aware chunks;
- symbol and definition indexes;
- deterministic natural-language query routing;
- bounded `peek`, `context`, and `full` result formats;
- content-hash embedding reuse;
- branch-aware membership;
- a conservative call graph;
- explicit cross-project code search;
- a versioned golden-query evaluation harness.

The result must preserve Awoki's existing guarantees:

- source eligibility remains fail-closed;
- secret, raw HTTP, key, environment, symlink, and unsafe-path exclusions run before parsing or embedding;
- repository search remains distinct from continuity and project memory;
- Qdrant remains derived and rebuildable;
- exact search continues to work during embedding or Qdrant failures;
- results remain navigation/evidence candidates until the authoritative source is opened;
- no repository code is executed during indexing.

---

## 2. Current baseline

The current repository-search path is:

```text
eligible code files
→ fixed 8,000-character chunks
→ project SQLite FTS
→ project Qdrant collection
→ exact source scan
→ weighted reciprocal-rank fusion
→ optional reranking
```

`codebase_search` explicitly enables project code indexing and then merges:

- SQLite FTS results;
- current Qdrant results filtered to `kind=code`;
- exact local source matches;
- optional reranking.

This is a sound retrieval foundation, but fixed character windows do not preserve function/class boundaries and the current project-wide rebuild path cannot reuse unchanged symbol embeddings efficiently.

The implementation should therefore introduce a **dedicated code index**, while keeping the existing general project-memory index unchanged.

---

## 3. Target architecture

```text
Awoki indexing policy
        │
        ├── reject unsafe file
        │
        └── allow safe source bytes
                │
                ▼
        language detection
                │
        ┌───────┴────────┐
        │ supported      │ unsupported / parse failure
        ▼                ▼
 Tree-sitter parser   deterministic text fallback
        │
        ▼
 symbols + chunks + references
        │
        ├── SQLite code metadata / FTS / graph
        │
        └── Qdrant content-addressed vectors
                │
                ▼
 deterministic query router
        │
        ├── definition lookup
        ├── exact identifier search
        ├── hybrid conceptual search
        ├── callers / callees
        ├── graph path
        └── cross-project search
                │
                ▼
      peek / context / full response
```

### Separation from project RAG

The new code subsystem should use separate derived stores:

```text
workspace/projects/<project>/index/sqlite/awoki_code.sqlite
```

and a dedicated Qdrant collection, for example:

```text
awoki_code_jina_embeddings_v2_base_code_768_v1
```

General project memory, reports, continuity, and safe artifacts continue using the existing project FTS and Qdrant path.

This prevents:

- code chunks from appearing as continuity memories;
- code-specific schema changes from destabilizing project RAG;
- project refreshes from re-embedding every source symbol;
- code branch membership from complicating memory points.

---

## 4. Dependency strategy

Add a small parsing abstraction around the official Python Tree-sitter bindings.

### Initial dependency model

Use:

```text
tree-sitter
```

plus a pinned, curated set of grammar packages for the languages Awoki officially supports at launch.

Initial language set:

```text
Python
JavaScript
TypeScript
TSX
Go
Rust
Java
C
C++
C#
Bash
Ruby
PHP
SQL
```

The grammar registry must be explicit and versioned. Awoki must not download grammars when MCP starts or when a repository is indexed.

Unsupported languages fall back to the existing deterministic text chunker and are clearly marked:

```text
parse_mode: text_fallback
parse_reason: unsupported_language
```

### Why not a runtime language downloader

Awoki is security-sensitive and intended to be reproducible. Runtime parser downloads would introduce:

- network dependency during indexing;
- mutable parser behavior;
- additional supply-chain surface;
- non-reproducible backup/restore results.

### Compatibility checks

At image build and validation time:

- import every configured grammar;
- instantiate a parser;
- parse a tiny fixture;
- verify grammar ABI compatibility;
- record parser and grammar versions in the code-index manifest.

If one grammar fails, validation must identify that language. It must not silently disable all structural parsing.

---

## 5. New package layout

Create a focused package rather than growing `harness_core.py` further:

```text
.harness/code_search/
  __init__.py
  models.py
  languages.py
  parser.py
  chunker.py
  queries.py
  store.py
  embeddings.py
  graph.py
  router.py
  service.py
  evaluation.py
  query_files/
    python/
      symbols.scm
      calls.scm
      imports.scm
    javascript/
    typescript/
    ...
```

Responsibilities:

### `models.py`

Typed dataclasses for:

```text
RepositoryIdentity
BranchIdentity
ParsedFile
CodeSymbol
CodeChunk
CodeReference
CodeEdge
CodeSearchRequest
CodeSearchHit
CodeSearchResponse
```

### `languages.py`

- extension-to-language mapping;
- grammar loader;
- grammar/version identity;
- supported-language status;
- fallback selection.

### `parser.py`

- parse safe source bytes;
- run Tree-sitter queries;
- return symbols, imports, calls, inheritance, and parse diagnostics;
- apply node/depth/chunk limits.

### `chunker.py`

- construct symbol chunks;
- attach leading comments/docstrings;
- create bounded context;
- split oversized symbols deterministically;
- produce stable chunk hashes.

### `store.py`

- SQLite schema and migrations;
- incremental file replacement;
- symbol/definition lookup;
- lexical search;
- branch membership;
- graph edge storage.

### `embeddings.py`

- compute embedding identity;
- look up reusable vectors;
- embed only missing content hashes;
- maintain Qdrant memberships;
- remove stale memberships safely.

### `graph.py`

- conservative symbol resolution;
- callers/callees;
- bounded path search;
- confidence and ambiguity reporting.

### `router.py`

- deterministic intent detection;
- explicit mode override;
- routing explanation.

### `service.py`

- public operations used by MCP tools;
- index freshness;
- result fusion;
- response shaping.

### `evaluation.py`

- load golden query sets;
- execute backends;
- calculate metrics;
- emit JSON and Markdown reports.

---

## 6. Structural chunk model

Each indexed chunk should represent a logical source unit where possible.

Examples:

```text
module-level function
class
method
interface
constructor
test case
configuration declaration
top-level constant group
```

### Required chunk metadata

```json
{
  "project_id": "payment-api",
  "repo_id": "payment-api:repo",
  "branch_key": "main",
  "commit": "abc123...",
  "working_tree_hash": "...",
  "path": "src/auth/service.py",
  "language": "python",
  "symbol_id": "...",
  "symbol_name": "validate_claims",
  "qualified_name": "auth.service.validate_claims",
  "symbol_kind": "function",
  "parent_symbol_id": null,
  "start_line": 41,
  "end_line": 67,
  "signature": "def validate_claims(payload: dict) -> None",
  "content_hash": "...",
  "embedding_key": "...",
  "parser_id": "tree-sitter-python:<version>",
  "parse_mode": "tree_sitter"
}
```

### Stable identity

Separate three concepts:

```text
symbol occurrence ID
    project/repo/branch/path/range identity

content hash
    normalized chunk bytes

embedding key
    deployment identity + model + dimension + normalization + content hash
```

A symbol can move or appear on several branches while reusing the same vector.

### Oversized symbols

A single very large class or function must not become one unbounded chunk.

Policy:

1. preserve the symbol header and docstring;
2. split by direct child statements or methods;
3. add limited parent context;
4. use deterministic line/byte boundaries;
5. never overlap excessively;
6. record `chunk_part` and `chunk_total`.

### Unsupported or malformed files

Use deterministic line-aware fallback chunks with:

```text
parse_mode: text_fallback
```

Fallback must preserve:

- path;
- line range;
- content hash;
- branch membership;
- normal safety filtering.

---

## 7. SQLite code index

Create one code-index database per project:

```text
workspace/projects/<project>/index/sqlite/awoki_code.sqlite
```

Suggested schema:

### `code_files`

```text
file_id
project_id
repo_id
branch_key
commit_sha
working_tree_hash
path
language
content_hash
size_bytes
parser_id
parse_mode
parse_status
indexed_at
```

Unique key:

```text
(project_id, repo_id, branch_key, path)
```

### `code_symbols`

```text
symbol_id
file_id
name
qualified_name
kind
signature
parent_symbol_id
start_byte
end_byte
start_line
end_line
content_hash
```

Indexes:

```text
name
qualified_name
kind
file_id
```

### `code_chunks`

```text
chunk_id
symbol_id
file_id
chunk_part
start_line
end_line
title
text
content_hash
embedding_key
```

A dedicated FTS5 table indexes:

```text
qualified name
symbol name
signature
path
chunk text
```

### `code_references`

```text
reference_id
file_id
source_symbol_id
reference_kind
target_name
target_qualified_hint
line
column
resolution_status
resolved_symbol_id
confidence
```

### `code_edges`

Materialized graph edges:

```text
edge_id
source_symbol_id
target_symbol_id
edge_kind
line
confidence
resolution_method
```

### `code_index_state`

```text
schema_version
engine_version
project_id
repo_id
branch_key
source_probe_hash
document_set_hash
parser_profile_hash
embedding_profile_hash
indexed_at
```

All schema upgrades must be versioned and rebuildable. The index is derived state, so migration failure may discard and rebuild this database, but must never modify repository source or continuity.

---

## 8. Content-hash vector reuse

This feature should be implemented without storing duplicate vectors for identical chunks on every branch.

### Qdrant point model

Use one point per unique `embedding_key`.

The payload contains:

```json
{
  "point_kind": "code_chunk",
  "embedding_key": "...",
  "content_hash": "...",
  "language": "python",
  "symbol_kind": "function",
  "project_ids": ["project-a", "project-b"],
  "repo_ids": ["project-a:repo", "project-b:repo"],
  "branch_keys": ["project-a:repo:main", "project-b:repo:release"],
  "memberships": [
    {
      "project_id": "project-a",
      "repo_id": "project-a:repo",
      "branch_key": "main",
      "path": "src/a.py",
      "symbol_id": "...",
      "start_line": 10,
      "end_line": 20
    }
  ]
}
```

### Index algorithm

For every new chunk:

1. calculate `embedding_key`;
2. check whether the Qdrant point already exists;
3. if it exists, update memberships without embedding;
4. if it does not exist, call TEI once and create the point;
5. remove stale memberships for deleted/moved branch occurrences;
6. delete the point only when no memberships remain.

### Concurrency

Qdrant membership updates require project-level indexing locks.

Use the existing lock discipline or add:

```text
workspace/projects/<project>/index/code-index.lock
```

The update should be retryable and idempotent.

### Failure behavior

If Qdrant or TEI is unavailable:

- update SQLite metadata and FTS;
- report vector indexing as degraded;
- retain pending embedding keys;
- allow a later refresh to complete missing vectors;
- never mark the vector document-set hash current until membership updates succeed.

---

## 9. Branch-aware membership

### Branch identity

For a Git repository, record:

```text
worktree root
current branch
HEAD commit
dirty source probe hash
```

Branch key:

```text
branch:<branch-name>
```

Detached state:

```text
detached:<full-commit-sha>
```

Non-Git repository:

```text
working-tree:<source-probe-hash>
```

### Default behavior

`/codebase` searches only the currently checked-out branch/worktree.

Results must include:

```text
project
repo
branch
commit
dirty state
path
symbol
line range
```

### Switching branches

When a branch changes:

- unchanged chunk vectors are reused;
- membership for the new branch is added;
- prior branch membership is retained;
- default searches filter to the active branch;
- deleted files on the active branch disappear immediately after refresh.

### Cleanup

Add a maintenance operation to prune branch memberships no longer present in local Git references, with preview-before-apply behavior.

No branch data should be deleted merely because the operator temporarily checked out another branch.

---

## 10. Definition and symbol lookup

Add exact structural lookup before semantic search.

Lookup precedence:

```text
exact qualified name
exact case-sensitive symbol name
exact case-insensitive symbol name
prefix match
lexical signature/path match
semantic fallback
```

New MCP tool:

```text
code_definition(
    symbol,
    name="",
    project_id="",
    branch="",
    language="",
    view="context",
    limit=10
)
```

Return ambiguity explicitly.

Example:

```json
{
  "status": "ambiguous",
  "symbol": "validate",
  "candidates": [...]
}
```

Do not arbitrarily choose one definition when several have equal structural relevance.

---

## 11. Deterministic query router

Keep `/codebase` as the normal front door.

Enhance `codebase_search` with:

```text
mode: auto | conceptual | exact | definition | callers | callees | path | similar
view: peek | context | full
```

Default:

```text
mode=auto
view=context
```

### Routing examples

```text
"Where is validate_claims defined?"
→ definition

"Find all uses of EXPECTED_ISSUER"
→ exact identifier

"Who calls mark_delivery_processed?"
→ callers

"What does webhook_handler call?"
→ callees

"Can request data reach subprocess.run?"
→ path

"How are duplicate webhook deliveries prevented?"
→ conceptual hybrid search
```

### Router rules

The first router must be deterministic and testable, using:

- explicit mode override;
- quoted identifiers;
- identifier-shape detection;
- stable phrase patterns;
- presence of two endpoints for path questions;
- fallback to conceptual search.

Do not make routing depend on another LLM call.

Every response should report:

```text
selected mode
routing reason
scope
branch
index freshness
backends used
degraded state
```

If the router is uncertain, conceptual hybrid search is the safe fallback.

---

## 12. Bounded result formats

### `peek`

Use for locating likely code with minimal context.

Returns:

```text
project
branch
path
symbol
kind
line range
one-line reason
backend/rank
```

No large source body.

Default budget:

```text
approximately 150–300 characters per hit
```

### `context`

Default `/codebase` format.

Returns:

- top symbols;
- signatures;
- bounded source excerpts;
- limited parent/import context;
- enough evidence to decide which files to open.

Default total budget should be configurable, for example:

```text
AWOKI_CODE_CONTEXT_MAX_CHARS=16000
```

### `full`

Returns complete selected symbol bodies, not entire arbitrary files.

Hard limits remain:

```text
maximum hits
maximum bytes per symbol
maximum total bytes
```

If the symbol is larger than the limit, return truncation metadata and recommend `open_artifact`.

---

## 13. Hybrid ranking

Conceptual search should combine:

```text
symbol-aware SQLite FTS
exact identifier/path candidates
Qdrant semantic candidates
structural boosts
optional TEI reranking
```

Structural boosts:

```text
exact symbol match
qualified-name match
path match
definition over incidental reference
test match when query asks for tests
active branch membership
```

Use deterministic weighted reciprocal-rank fusion before reranking.

Reranking remains optional and bounded.

The response must distinguish:

```text
retrieved by FTS
retrieved by Qdrant
retrieved by exact lookup
boosted structurally
reranked
```

---

## 14. Conservative call graph

Tree-sitter provides syntax, not full compiler semantics. The call graph must therefore report confidence.

### Extract

Per language:

- function/method definitions;
- call expressions;
- imports/includes;
- class inheritance where practical;
- receiver text and qualified hints.

### Resolve in stages

1. same-symbol exact local resolution;
2. same-file resolution;
3. import-aware module resolution;
4. unique project-wide name resolution;
5. ambiguous candidate set;
6. unresolved reference.

Edge confidence:

```text
high
medium
low
```

Resolution status:

```text
resolved
ambiguous
unresolved
```

### Tools

```text
code_callers(symbol, ...)
code_callees(symbol, ...)
code_path(source, target, ...)
```

`code_path` should:

- use bounded BFS;
- default to resolved edges;
- optionally include ambiguous edges;
- cap depth and explored nodes;
- state that dynamic dispatch/reflection may be missing.

A graph path is supporting evidence, not proof that the runtime executes that path.

---

## 15. Explicit cross-project search

Add:

```text
cross_project_code_search(
    query,
    projects=[],
    all_indexed=false,
    mode="auto",
    view="context",
    limit=20,
    refresh_stale=false
)
```

### Safety rules

- no implicit all-project search;
- require either a non-empty project list or `all_indexed=true`;
- only search projects whose code indexing policy is enabled;
- do not automatically index stale projects unless `refresh_stale=true`;
- return per-project freshness and degradation;
- label every hit with project/repo/branch/path/symbol;
- preserve each project's file eligibility policy;
- never include global memory or project continuity in this operation.

### User command

```text
/code-across payment-api,webhook-service How is replay protection implemented?
```

Explicit all-indexed form:

```text
/code-across --all-indexed Where is JWT issuer validation configured?
```

The command prompt should make the scope visible before calling the tool.

---

## 16. User-facing commands

Keep `/codebase` as the default.

Expose:

```text
/codebase <natural-language question, including requested result depth>
/definition <symbol>
/callers <symbol>
/callees <symbol>
/code-path <source> -> <target>
/code-across <projects> <question>
/code-validate-claim <atomic claim or broad source-logic verification request>
/code-index-status
```

`peek`, `context`, and `full` remain engine views selected from natural-language `/codebase` wording rather than separate slash commands. Golden evaluation remains a maintainer Make/MCP operation rather than a normal user command.

### Natural-language behavior

Examples in command documentation should teach:

```text
/codebase How are duplicate callbacks suppressed?
/definition should_process_delivery
/callers mark_delivery_processed
/code-path webhook_handler -> execute_payment
/code-across billing-api,worker-service Where is idempotency enforced?
```

Commands should not require users to understand FTS, Qdrant, Tree-sitter, or RRF.

Technical diagnostics remain available in result metadata.

---

## 17. MCP/API evolution

### Preserve

```text
codebase_search
open_artifact
project_index
project_refresh
retrieval_status
retrieval_probe
```

### Extend `codebase_search`

Add backward-compatible optional parameters:

```text
mode="auto"
view="context"
branch=""
language=""
symbol_kind=""
max_chars=0
```

### Add tools

```text
code_index_status
code_index_verify
code_definition
code_callers
code_callees
code_path
code_flow_graph
code_source_window
cross_project_code_search
code_evaluate
```

`code_flow_graph` and `code_source_window` are internal investigation primitives,
not additional slash commands. The former returns a bounded relevant reachable
graph from one exact symbol and traverses resolved calls only. The latter returns
active-branch, full-file-hash-checked source under hard total/per-line bounds so
pathological giant lines cannot force unbounded MCP output.

### Compatibility

Existing calls with only:

```text
query
name
limit
refresh_index
session_id
```

must behave normally.

There is one production code-search engine. Development and acceptance happen
on a release branch against frozen baseline artifacts; no runtime switch or
parallel production index is retained.

Existing `codebase_search` calls remain API-compatible while their internals use
the native structural index. Operational degradation is limited to durable
resilience paths that are part of the final design:

```text
unsupported or malformed grammar → deterministic text/Python-AST fallback
Qdrant or embeddings unavailable → exact structural lookup plus SQLite FTS
reranker unavailable → deterministic fused order without reranking
```

---

## 18. Manifest and freshness

Upgrade the project index manifest or add a dedicated code manifest:

```text
workspace/projects/<project>/index/manifests/code-index.json
```

Suggested fields:

```json
{
  "schema_version": 1,
  "engine": "awoki-structural",
  "engine_version": "...",
  "project_id": "...",
  "repo_id": "...",
  "branch_key": "...",
  "commit_sha": "...",
  "dirty": true,
  "source_probe_hash": "...",
  "parser_profile": {...},
  "parser_profile_hash": "...",
  "embedding_profile": {...},
  "embedding_profile_hash": "...",
  "symbol_count": 0,
  "chunk_count": 0,
  "edge_count": 0,
  "fallback_file_count": 0,
  "embedding_reused_count": 0,
  "embedding_created_count": 0,
  "document_set_hash": "...",
  "sqlite": {...},
  "qdrant": {...},
  "indexed_at": "..."
}
```

Freshness requires all of:

```text
active branch identity matches
source probe hash matches
parser profile hash matches
embedding profile hash matches
SQLite document-set hash matches
Qdrant membership hash matches
```

A current SQLite index with failed Qdrant updates must be reported as:

```text
lexical_current: true
vector_current: false
degraded: true
```

---

## 19. Golden-query evaluation

Create:

```text
.harness/evaluation/code_search/
  suites/
    smoke.jsonl
    conceptual.jsonl
    structural.jsonl
    graph.jsonl
    cross_project.jsonl
    security.jsonl
    branches.jsonl
  fixtures/
  reports/
```

Each query record:

```json
{
  "id": "webhook-duplicate-concept",
  "projects": ["webhook-service"],
  "query": "How does the service decide whether an incoming callback was already handled?",
  "mode": "conceptual",
  "expected": [
    {
      "project_id": "webhook-service",
      "path": "src/webhook_worker.py",
      "symbol": "should_process_delivery",
      "grade": 3
    },
    {
      "path": "tests/test_webhook_worker.py",
      "grade": 2
    }
  ],
  "forbidden_paths": [
    ".env",
    "captures/raw.http"
  ],
  "expected_no_answer": false
}
```

### Metrics

Report:

```text
Hit@1
Hit@3
Hit@5
MRR@10
nDCG@10
no-answer precision
forbidden-path leakage
cross-branch leakage
p50/p95 search latency
cold index time
incremental index time
embedding calls
new vectors
reused vectors
response characters
```

### Baseline comparison

Before replacing the old implementation, capture its results once from the
parent commit into a versioned, immutable baseline report. Development and CI
then compare the current native structural engine with that stored historical
artifact. The old engine is not executed or shipped as a production fallback.

Store deterministic JSON and Markdown reports with the baseline commit, query
suite hash, environment identity, and observed limitations.

### Acceptance gates

Before structural becomes default:

1. zero forbidden-file results;
2. zero cross-branch leakage;
3. unchanged chunks produce zero new embedding calls;
4. deleted active-branch code does not remain current;
5. exact symbol lookup meets or exceeds the frozen pre-structural baseline;
6. conceptual top-rank quality improves on the agreed golden set;
7. no significant Hit@5 regression against the frozen baseline;
8. warm search latency remains within configured budget;
9. all existing Awoki tests pass;
10. all new structural, graph, branch, and cross-project tests pass.

The exact performance thresholds should be set from the first reproducible baseline rather than invented before measurement.

---

## 20. Testing strategy

### Unit tests

- language detection;
- parser loading;
- symbol extraction by language;
- docstring/comment attachment;
- oversized-symbol splitting;
- deterministic chunk IDs;
- embedding-key stability;
- router intent selection;
- response budgets;
- graph resolution;
- branch identity;
- Qdrant membership merge/remove.

### Security tests

- symlink rejection before parsing;
- `.env`, key, certificate, raw HTTP, database, and binary exclusion;
- secret detection before embedding;
- path traversal rejection;
- unsupported grammar fallback;
- parser failure does not widen eligibility;
- cross-project scope requires explicit consent;
- results never include non-selected projects.

### Incremental tests

- unchanged file: no parse or embedding work where cache permits;
- one function changed: only affected chunks re-embedded;
- file renamed with identical content: vector reused;
- branch switch with shared code: vector reused;
- file deletion: membership removed;
- last membership removal: Qdrant point deleted.

### Graph tests

- direct same-file call;
- imported function call;
- ambiguous name;
- unresolved dynamic call;
- bounded path;
- graph does not claim certainty for ambiguous edges.

### Integration tests

- SQLite + mocked embedding provider;
- Qdrant test service where available;
- embedding dimension mismatch;
- Qdrant outage fallback;
- reranker outage fallback;
- fresh clone and image build;
- backup/restore invalidates or rebuilds derived code state correctly.

### Regression tests

All existing tests remain mandatory.

The public `codebase_search` API remains backward-compatible. Structural changes are merged only after the release branch passes the evaluation gate; no second runtime backend is retained.

---

## 21. Documentation changes

Add:

```text
docs/CODE_SEARCH.md
docs/CODE_SEARCH_EVALUATION.md
```

Update:

```text
README.md
docs/ARCHITECTURE.md
docs/CONTINUITY.md
docs/BACKUP_RESTORE.md
.harness/HARNESS.md
.harness/manifest.json
.opencode/skills/project-continuity/SKILL.md
.opencode/commands/codebase.md
.opencode/commands/retrieval-status.md
```

Documentation must explain in plain language:

- use `/codebase` for ordinary repository questions;
- repository analysis is evidence-backed by default; users do not need to add
  "deterministically" to ordinary explain/trace/understand requests;
- use `/definition` when the symbol is known;
- use `/callers` and `/callees` for direct relationships;
- use `/code-path` for a possible path between symbols;
- use `/code-across` only when multiple projects are intentionally in scope;
- use indexed exact search before raw grep; if structural discovery genuinely
  cannot locate a construct, use `.harness/bin/code-search-fallback`, which finds
  file names first and emits bounded, wall-clock-limited previews rather than unrestricted `rg --json`;
- use `code_flow_graph` for a bounded relevant subgraph, not an all-repository graph dump;
- use `code_source_window` for bounded hash-checked authoritative source before
  making implementation claims;
- inspect branch predicates, local assignments/aliases, arguments, returns, and
  outcomes because a call graph alone is not full control/data flow;
- use strict `code_validate_claim` selectively for supported atomic propositions
  underneath broader deterministic investigation;
- call graphs are conservative approximations, especially in dynamic languages;
- semantic indexes are derived and may degrade without losing project continuity.

---

## 22. Backup and restore impact

Portable backups should include:

```text
code-index manifest
golden-query definitions
evaluation configuration
```

They should exclude rebuildable:

```text
awoki_code.sqlite
Qdrant vectors
evaluation output caches
```

Full backups may include those derived stores, but compatibility checks must include:

```text
code index schema version
parser profile hash
grammar versions
embedding deployment identity
vector dimensions
Qdrant collection identity
```

An incompatible full restore should invalidate and rebuild code indexes rather than silently use mismatched vectors or parsers.

---

## 23. Implementation phases

### Phase 0 — Baseline and safeguards

Deliver:

- current code-search golden fixture;
- a frozen metric report produced from the pre-structural parent commit;
- a dedicated implementation branch;
- no production runtime switch.

Gate:

- existing test suite passes unchanged before implementation starts.

### Phase 1 — Structural parser and chunker

Deliver:

- dependency pins;
- language registry;
- Tree-sitter query files;
- symbol-aware chunks;
- unsupported-language fallback;
- parser diagnostics.

Gate:

- deterministic parser/chunk tests;
- security exclusions proven before parsing.

### Phase 2 — Dedicated SQLite code index

Deliver:

- schema;
- incremental file replacement;
- symbol/definition lookup;
- code FTS;
- code-index manifest/status.

Gate:

- exact search parity;
- deleted/renamed file correctness;
- no project-memory contamination.

### Phase 3 — Router and bounded responses

Deliver:

- `mode=auto`;
- `peek/context/full`;
- natural-language `/codebase` views plus `/definition`;
- backward-compatible `/codebase`.

Gate:

- deterministic routing suite;
- response-budget tests;
- natural-language command examples.

### Phase 4 — Qdrant content reuse and branches

Deliver:

- dedicated code collection;
- content-addressed vectors;
- membership updates;
- branch filtering;
- reuse counters;
- degraded lexical fallback.

Gate:

- unchanged chunks cause no embedding calls;
- branch leakage is zero;
- deletion removes active membership;
- Qdrant outage preserves lexical search.

### Phase 5 — Call graph

Deliver:

- call/reference extraction;
- conservative resolution;
- callers/callees;
- bounded path search;
- confidence reporting.

Gate:

- resolved/ambiguous/unresolved tests;
- no overstatement in dynamic-language fixtures.

### Phase 6 — Cross-project search

Deliver:

- explicit project-list search;
- explicit `--all-indexed`;
- per-project freshness;
- `/code-across`.

Gate:

- no implicit scope expansion;
- clear project/branch labels;
- no forbidden-file or project leakage.

### Phase 7 — Evaluation and default rollout

Deliver:

- golden suites;
- frozen-baseline-versus-current reports;
- CI quality gate;
- documentation;
- the single structural engine released after acceptance.

Gate:

- agreed retrieval-quality and safety thresholds;
- full Awoki suite passes;
- fresh clone, Docker build, runtime smoke, and clean bundle clone pass.

---

## 24. Rollback plan

There is no runtime legacy switch. Release rollback means reverting the
structural-search release commit or deploying the previous signed/bundled Awoki
release. Because the subsystem is derived and isolated, this must not require:

- changing continuity records;
- deleting project source;
- changing general project RAG;
- restoring project memory from backup.

The dedicated SQLite database and Qdrant code collection may be deleted and
rebuilt after preview/confirmation. A rollback release must rebuild derived code
indexes using the schema and parser profile shipped by that release rather than
trying to reinterpret newer index files.

---

## 25. Recommended implementation order

The order should remain:

```text
evaluation baseline
→ parser/chunker
→ SQLite symbols/definitions
→ router and bounded views
→ content-hash vectors
→ branch membership
→ call graph
→ cross-project search
→ final evaluation and rollout
```

Do not implement call graphs or cross-project search before structural identity, branch scope, and index freshness are trustworthy.

The most important early milestone is:

> `/codebase` returns structurally coherent symbols from a dedicated Awoki code index while preserving exact fallback and all existing safety exclusions.

The most important final milestone is:

> The same versioned query suite proves that structural Awoki search improves code navigation without cross-project, cross-branch, stale-document, or sensitive-file leakage.
