# Native structural code search

Awoki has one native repository-search engine. It does not invoke a second
code-index service or maintain competing search backends.

## Normal use

Ask in natural language:

```text
/codebase How are duplicate webhook deliveries prevented?
```

`codebase_search` routes the question deterministically. It may choose:

- lexical FTS-only search;
- conceptual hybrid search;
- exact identifier or literal search;
- definition lookup;
- callers or callees;
- a bounded statically resolved path;
- similar-code retrieval.

The response reports the selected mode, routing reason, project, repository,
active branch, commit, dirty state, repository assurance, index freshness,
backends used, and any degradation.

For ordinary users, `mode=auto` remains the default. For repeatable retrieval
experiments and diagnostics, `codebase_search` also accepts `mode=lexical`,
`use_fts`, `use_qdrant`, `use_reranker`, `structural_promotion`,
`result_focus=auto|implementation|balanced|tests|config`, and
`strict_backends`. Unknown explicit modes are rejected. With
`strict_backends=true`, a requested Qdrant/reranker path must actually succeed;
Awoki does not silently substitute a degraded path and call the experiment
successful.

Projects may use the legacy exact root at `repo/` or multiple registered exact
roots such as `repo/oathkeeper` and `repo/hydra`. Broad `codebase_search` spans all
enabled registered repositories. Exact definition/source/graph/evidence/semantics
and exhaustive operations accept `repo=` and reject ambiguous multi-repo requests
instead of selecting a checkout arbitrarily.

Repository evidence is layered rather than reduced to one “Git truth” bit:

```text
repository root identity
  -> Git commit/tree/view identity
  -> declared search universe + completeness
  -> exact file bytes / evidence id
  -> structural relationships
  -> language/runtime primitive observation when needed
  -> model interpretation
```

`VERIFIED_SNAPSHOT` means an explicit index/verify operation established an exact
Git root and clean supported source snapshot. `WORKING_TREE_BOUND` means exact
source can still be inspected/hash-bound but the whole worktree is not claimed as
one immutable snapshot. `FILESYSTEM_BOUND` is intentional non-Git evidence.
Unusual Git state lowers assurance and is reported; it does not hide readable
source. Git author/committer fields are metadata claims, not verified people.

Repository understanding is evidence-backed by default. `codebase_search` is the
discovery stage, not the final evidentiary stage. Semantic/FTS/Qdrant results may
locate candidate implementation code but do not themselves establish behavior.
The returned `analysis_policy` tells clients whether a request appears
flow-oriented and recommends exact follow-up primitives.

### Authority-aware conceptual retrieval

Conceptual search is explicitly staged:

```text
query
  -> lexical FTS candidates
  -> current Qdrant semantic candidates
  -> reciprocal-rank fusion (raw stage ranks/scores retained)
  -> bounded verified structural expansion from strong test/config hits
  -> bounded concrete-symbol refinement from strong production containers
  -> focus-aware bounded reranker selection across broad/focus/refined lanes
  -> remote reranker over the selected window when enabled
  -> rank-based retrieval/reranker fusion
  -> query-intent authority prior
  -> deterministic diversity
  -> final top-K
```

Tests, fixtures, config/schema, generated/stub material, documentation, and
production source all remain searchable. Awoki does **not** globally exclude
tests: they are often the best semantic description of a boundary and can be the
correct target when the query asks for tests. For implementation/security/runtime
questions, however, relevant production functions/methods receive a scale-safe,
relevance-gated authority preference. The preference is multiplicative rather than
an absolute score bonus and is strongest when independent signals such as FTS +
Qdrant agreement or bounded query overlap support the candidate. This prevents a
weak unrelated production helper from jumping upward merely because it is
production code. A production implementation may receive one guarded top-three
representation slot only when its baseline relevance and independent support are
already sufficient.

R9.1.3 distinguishes concrete implementation from coarse production containers and
contracts. A module/file hit such as a Go file can be excellent discovery evidence
while still hiding the actual method the user needs. Strong implementation-focused
module/type hits are therefore refined through existing structural metadata into
bounded concrete functions/methods. Direct parent/child edges are preferred, while
module/file containers additionally use exact indexed file-scope enumeration as a
deterministic fallback. This covers receiver-style methods such as Go methods without
guessing from names. Each child is reranked using its own source window against the
original query. If a concrete child already exists in the broad discovery pool, the
existing candidate is requalified with the verified parent/refinement relationship
instead of being duplicated; this lets a deep concrete method receive a bounded
reranker opportunity even when it fell outside the global broad cutoff. Parent
relevance grants evaluation capacity only; a child without a returned reranker score
does not inherit the parent's rank into the final results.
Refinement telemetry records concrete children available, already represented by
broad discovery, generated, or omitted by explicit per-parent/total bounds; this
prevents a method from being misreported as a silent refinement gap. Interface-only
modules remain contracts/containers rather than receiving an implementation bonus
simply because they are production `.go` files.

R9.1.5 adds a language-neutral lexical normalization layer before semantic fusion.
The original query terms remain present, while deterministic aliases decompose
common code spelling boundaries such as `bearer-token`, `bearer_token`,
`BearerToken`, `getHTTPResponse`, paths/namespaces, and similar identifier forms.
Because FTS5 cannot match a word occurring only inside the middle of one camelCase
token, a bounded local identifier bridge also checks the already-indexed path,
symbol, qualified-name, signature, and chunk text and fuses that lexical order
with native FTS5. The bridge is local-only and literal: no stemming, semantic
expansion, source execution, or language-specific dictionary is involved. Parsed
languages such as Go, Java, JavaScript/TypeScript and text-fallback languages such
as Swift therefore share the same lexical behavior; unsupported structural
languages still do not gain invented symbols or call edges.

For `result_focus=implementation`, final composition is rank-only and relevance-gated.
A concrete production implementation may become the top implementation anchor only
when its post-authority relevance remains close to the semantic leader and an
independent signal supports it. A second independently strong implementation may
be retained inside the top five under a lower explicit relevance ratio. This is not
a role quota: no score is changed, and weak/unrelated production stays where its
retrieval evidence puts it. Explicit test/config focus bypasses this composition.

Structural expansion is candidate generation only. A verified call/reference
edge proves connectivity, not behavioral relevance. Promoted production
candidates receive bounded reranker capacity and must earn relevance against the
original query; without an independent reranker score their production-authority
bonus is capped by a conservative local query-overlap signal. This prevents a
test -> constructor/helper graph path from becoming “authoritative” merely
because it reaches production code.

Every final hit retains stage provenance where applicable: FTS/Qdrant raw score
and rank, fused score/rank, pre-rerank rank, rerank score/rank/backend,
reranker selection lane/reason, rank-fusion components/score,
symbol-refinement parent/reason/depth/requalification,
authority adjustment/class, diversity adjustment, final score/rank, and any
structural-promotion source/edge/distance. Search-level telemetry explicitly
reports whether reranking was requested, attempted, and actually applied. It also
separates the reranker pool/budget and broad/focus/refined/refill selection lanes,
reranker candidates selected/request documents, configured top-N, effective requested
top-N, explicit scores returned to Awoki, selected candidates without a returned
score, not-selected, and post-rerank pool counts. Only the selected window is sent to
the reranker; the untouched tail is preserved separately. Internal code search asks
for a score for every selected candidate rather than allowing a smaller configured
`top_n` return limit to pre-decide Awoki's final ranking. If the backend still returns
fewer scores, the shortfall remains explicit. `selected_without_returned_score` does not
claim that the backend failed to score the document internally because that is not
observable from the current rerank response contract. The model must never infer
reranker execution from score shapes or treat a raw reranker score as comparable
to a fused retrieval score.

The response also includes compact metadata-only `stage_top` previews for fused,
promoted, refined, reranked, and final candidates. Preview/source-text character budgets are shared
across the requested top-K; running out of preview budget never silently drops
ranked result metadata.

Exact structural operations remain optional precision controls:

```text
/definition validate_claims
/callers mark_delivery_processed
/callees webhook_handler
/code-path webhook_handler -> execute_payment
/code-across payment-api,worker-service How is idempotency enforced?
```

Result depth stays natural-language inside `/codebase`:

```text
/codebase locations only for issuer validation
/codebase explain duplicate callback handling with bounded context
/codebase show the complete implementation of duplicate callback handling
```

Cross-project scope is never implicit. `all_indexed=true` is accepted only when
the user explicitly asks for all indexed projects. Existing indexes are searched
as-is; stale projects are rebuilt only when `refresh_stale=true` is explicit.

Project opening and repository registration are passive for remote embeddings.
They return `repository_index_advice` with per-repository structural/vector
freshness. Existing stale structural snapshots are refreshed with detached
`code_index_refresh_start`; status exposes file totals/processed/parsed/reused,
current path, parse modes, elapsed time, and final lexical/vector freshness without
source text. `refresh_index=true` on MCP search starts the same local-only job and
returns rather than blocking on a full repository parse. After local indexing,
missing/stale vectors should be offered as an explicit `code_vector_refresh_start`
action. Both start calls are intentionally non-blocking; report the job id and return
control rather than autonomously polling. `code_vector_refresh_status` reports phase, chunks ready/total, target/reused/persisted vectors, batches completed/total, percentage, collection, and elapsed time without source text. Cancellation is explicit. Interactive search never silently materializes remote vectors. Before the first expensive embedding batch, Awoki materializes/validates the Qdrant code collection using the configured vector dimension, so storage/collection failures fail early. First-time embedding can be CPU-heavy; vector points are persisted incrementally so interrupted runs can reuse completed content-addressed chunks.

The job protocol also returns `recommended_poll_after_seconds` and
`next_poll_after`. Repeated status requests inside that interval return cached
progress with `poll_too_soon=true` and `retry_after_seconds`; they do not trigger
new expensive work. Failed refreshes retain actual persisted/reused vectors,
completed/failing batch telemetry, and remaining work. Full-reuse refreshes
batch Qdrant inventory and skip unchanged payload writes rather than issuing one
network mutation per already-current point.

Lexical and vector freshness are separate. The Qdrant membership hash and
`published_vector_collection` describe the last successfully published semantic
snapshot. A local FTS/parser rebuild with identical chunk membership preserves
that vector snapshot. A failed redundant/forced refresh also cannot destroy a prior
known-good snapshot when its semantic membership and collection still match; the
refresh job remains degraded for truthful diagnostics while search continues from
the last successfully published vectors. Source/chunk membership changes, semantic
embedding identity changes, branch/repository identity changes, or collection
identity changes mark vectors stale; operational knobs such as embedding batch
size/timeouts/retries do not.

## Deterministic investigation workflow

Natural-language requests such as “explain the flow”, “trace how this file is
processed”, “how does HTTP input reach persistence?”, or “understand this decision
tree” should normally execute this workflow without requiring the user to ask for
determinism explicitly:

```text
codebase_search
  -> exact entry-point definition
  -> code_flow_graph (bounded relevant reachable subgraph)
  -> code_source_window (hash-checked bounded source + evidence id)
  -> selective code_validate_claim checks for supported atomic propositions
  -> code_semantics_check for supported deterministic Go primitives when relevant
  -> evidence-labeled explanation
```

`code_flow_graph` is an internal MCP investigation primitive rather than another
slash command. It starts from one exact symbol, follows only `resolved` call
edges, and retains ambiguous/unresolved calls as explicit boundaries. It has hard
depth, node, and edge limits. The purpose is to map the relevant execution region,
not to dump the whole repository graph.

`code_source_window` is the authoritative source-reading primitive for indexed
files. Before returning a range it re-reads the current active-branch file and
requires its full content hash to match the indexed policy-approved bytes. It
then applies a total response cap, a line-count cap, and a per-line character cap.
This makes very long generated/minified/serialized source lines explicit
truncation events rather than transport failures. The response includes a bounded
`truncation` object naming the reason (`max_chars`, `max_lines`, or
`max_line_chars`), whether the requested range was complete, any clipped line
numbers, and `continue_from_line`/a suggested continuation when more requested
lines remain. Do not silently treat a truncated window as the complete range.

Every successful source window also returns an `evidence_id` binding the project,
commit/raw-tree identity when available, mutable Git-view fingerprint, path/range,
exact source SHA-256, and the HEAD blob OID for tracked Git source. Use
`code_evidence_verify` before reusing important old source evidence after edits,
branch/ref/view changes, or a long-running investigation. The ID is compact,
self-contained, and checksum-protected for stale/corruption detection; it is not
cryptographically signed and does not attest repository origin or human
authorship.

For supported Go language/stdlib primitives, use `code_semantics_check` instead
of model memory. It currently covers `path.Join`, `path.Clean`,
`time.ParseDuration`/duration multiplication, failed `error` type assertion,
bounded `strings.Replace`, `url.Parse`, and `httputil.ReverseProxy` Rewrite-entry
forwarded-header behavior. Docker executes a small fixed stdlib-only helper
precompiled by the pinned Go builder stage; source-tree development may compile
that same fixed helper locally. Repository code is never compiled/executed and the
helper has no network path. It reports the actual helper Go toolchain and the
attached project's plain-text `go.mod` declaration. A Go major/minor mismatch is
an explicit evidence boundary for version-sensitive stdlib behavior.

For a flow explanation, inspect more than function calls. Relevant current source
should be used to trace branch predicates, local assignments and aliases,
arguments, transformations, returns, and terminal outcomes. When the strict
validator supports an important bounded proposition, validate it automatically;
otherwise label the directly inspected result as source-confirmed rather than
pretending the strict proof grammar covered it.

Suggested evidence labels are:

```text
VERIFIED          strict code_validate_claim proof succeeded
SOURCE-CONFIRMED  exact current source/structure supports the statement but it is outside the strict proof grammar
AMBIGUOUS         multiple legitimate structural targets remain
INCONCLUSIVE      dynamic/unsupported/stale evidence prevents confirmation
DISCOVERY ONLY    semantic/lexical candidate not yet confirmed from source
```

These labels do not claim that a static source path executed at runtime. Runtime
claims still require observed runtime evidence.

## Exact lexical search and exhaustive fallback

Use the tool that matches the question:

```text
conceptual/architectural discovery
  -> Awoki structural/indexed discovery

known string/symbol lookup
  -> OpenCode native Grep

complex/exhaustive exact enumeration
  -> Awoki `code_exact_search` when full ripgrep-style features materially help without Bash

machine-checked exhaustive coverage / transport recovery
  -> Awoki code_text_search
```

`code_exact_search` is Awoki's first-class structured ripgrep interface for normal exact enumeration. It accepts typed patterns, paths, include/exclude globs, case/fixed-string mode, context, hidden/ignored policy, and `matches` / `files` / `count` modes; it does not accept arbitrary raw CLI fragments or shell command strings. Repository-facing `rg` runs with retrieval/provider credentials and ambient loader/helper overrides stripped, and explicit sensitive-file matches are redacted at transport. Bounded pages expose `has_more`, `truncated`, and `continuation.next_offset`.

For broad security/architecture questions, indexed discovery remains the better first move. OpenCode Grep is preferred when its smaller structured interface is sufficient; `code_exact_search` is the next step when full ripgrep-style control is useful without shell construction. Neither exact-search tool is authoritative proof by itself: reopen the relevant source and preserve exact evidence when a conclusion matters. Active acceptance contracts may intentionally restrict tools; those test contracts override normal ergonomics.

One giant matching line can still exceed a client JSON-record limit even when
`code_exact_search` itself can establish the exact enumeration. `code_text_search` remains the deterministic
coverage path when exact-search transport is incomplete or the claim requires a
machine-visible exhaustive repository universe.

`code_text_search` is Awoki's deterministic ripgrep interface. Its contract is:

- scan every textual source/config candidate in the requested repository-relative scope except explicit hard
  source-policy exclusions (for example a source-level `awoki:no-rag` marker, unsafe symlink, or binary/non-text file); large textual files remain lexical-only rather than disappearing, and security vocabulary or ordinary credential-handling expressions never
  exclude an otherwise valid source file;
- never cap total candidate files or total matches merely to save tokens;
- reuse the structural index manifest for eligibility on a clean Git snapshot
  when that manifest describes the exact active commit, avoiding a redundant
  repository-wide read/hash/redaction pass;
- materialize each exhaustive query/source-snapshot result once in an internal
  SQLite search cache; later pages read that materialized result instead of
  rerunning ripgrep or repository policy scanning;
- return final total match and matching-file counts once `scan_complete=true`;
  while discovery is still resumable, `match_count_final=false` makes the
  partial nature of the current count explicit;
- paginate the returned representation using `next_cursor`, and also use the
  cursor to resume unfinished discovery when the operation wall-clock budget is
  reached;
- bind cursors to the current branch/search/source snapshot so source changes
  return `stale_cursor` instead of mixing pages from different worktrees;
- return line/column/byte offsets, bounded match/context previews, and line byte
  length so giant lines never need to be serialized whole;
- split timed-out ripgrep shards automatically down to individual files;
- bound one MCP invocation with `operation_timeout_seconds` (20 seconds by
  default). Reaching that soft deadline returns `scan_complete=false`,
  `resume_required=true`, and a `next_cursor` rather than waiting for the MCP
  client to terminate the request;
- report `eligible_universe_complete=false` if an eligible file still cannot be scanned within
  the shard bound; separately report `repository_universe_complete=false` whenever any source file
  in scope is policy-excluded, and expose the excluded-source count/reasons; `universe_complete` is
  retained as a compatibility alias for repository-source completeness;
- treat every result as `DISCOVERY ONLY`.

A caller should continue with `next_cursor` until:

```text
scan_complete=true
search_complete=true
repository_universe_complete=true
```

`scan_complete=false` means discovery itself still has pending files and must be
resumed with `next_cursor`. `search_complete=false` with `scan_complete=true`
means discovery is complete but later result pages remain.
`eligible_universe_complete=false` means an eligible file timed out or failed.
`repository_universe_complete=false` means either that happened or one or more source files were
explicitly policy-excluded; Awoki must not claim whole-repository source coverage in either case.


### Coverage-first repository universe

Awoki separates **structural parser support** from **lexical repository coverage**. Exhaustive text search enumerates repository textual candidates first and applies explicit policy with accounting. A file may be `structural_parser=none` and still be lexically searchable. No path is silently removed merely because its directory is named `.harness`, `.opencode`, `auth`, `credentials`, or `secrets`. Policy exclusions are returned with reasons and make whole-repository completeness false.

Each configured repository root must match its own Git worktree top-level when Git is present. In legacy mode that root is the project `repo/` directory; in registered mode it is the selected child such as `repo/oathkeeper/`. A parent-worktree mismatch or accidental nested clone returns `invalid_repo_root`; Awoki does not silently bind source evidence to the wrong repository or commit identity.

For Git worktrees, the default repository universe is **tracked files plus visible untracked files**, honoring `.gitignore`. `code_text_search` reports this as `repository_scope=git_tracked_and_visible_untracked` and `git_ignored_paths_scanned=false`. Therefore `repository_universe_complete=true` means complete over that declared Git repository scope, not over ignored filesystem artifacts. When explicit forensic work requires ignored untracked files too, set `include_ignored=true`; the scope becomes `git_tracked_visible_untracked_and_ignored`, ignored paths are snapshot-bound into cursors, and explicit sensitive files such as `.env` still return opaque match/context previews. Non-Git projects report `repository_scope=filesystem_textual_candidates`.

### Source-code secret handling

Source code is not treated like captured traffic or raw credential material. Identifiers and expressions such as
`token := parseToken(r)`, `password = config.password`, JWT/OAuth handlers, and packages named
`credentials`/`secrets` are normal analysis input and remain indexable/searchable. Explicit
sensitive textual data-file types (`.env`, private keys, credential stores, etc.) are not structurally indexed or embedded, but exhaustive local repository text search may still count/search them with opaque match/context previews so their existence cannot create a hidden false negative. Tracked textual generated/vendor paths are likewise lexical-only rather than invisible. Explicit `awoki:no-rag` remains the intentional user-controlled exclusion and makes `repository_universe_complete=false`. Parser support does not define lexical coverage. Normal textual non-prose source/config files—including unknown extensions—also participate in primary code discovery using deterministic `text_fallback` chunks when no dedicated parser exists; prose/log/tabular documents stay lexical-only to avoid polluting the code graph. For ordinary source files, high-confidence actual credential literals are redacted best-effort from chunks, reference text, source-window output, claim-validation evidence, and text-search previews while the file, symbols, locations, and call graph remain available. This is a coverage-first policy, not a zero-leak guarantee.

Multiple roots are supported, for example:

```text
code_text_search(
  pattern="ProcessTree|loadTree|execute.*tree",
  paths=["src", "pkg", "resources"],
  page_size=1000
)
```

Do not pipe this workflow through `head`, `tail`, or another arbitrary truncation
layer; use the returned cursor.

If the MCP layer itself is unavailable, the diagnostic CLI provides the same
logical model:

```bash
.harness/bin/code-search-fallback 'ProcessTree|loadTree|execute.*tree' \
  workspace/projects/PROJECT/repo/oathkeeper/src \
  workspace/projects/PROJECT/repo/oathkeeper/pkg
```

The helper accepts `PATH [PATH ...]`, optional `--include-ignored`, reports complete match/file counts, and
returns a bounded page with `next_cursor`; request later pages with
`--cursor <offset>`. It does not use ripgrep JSON records and therefore does not
serialize multi-megabyte source lines.

Lexical fallback is discovery, not behavioral proof. Reopen relevant eligible
source with `code_source_window` and structural tools before asserting behavior.
If the only relevant material is policy-excluded or remains unindexed, report
that evidence boundary as `INCONCLUSIVE` rather than bypassing the source policy.

## Indexing model

Eligible structural source passes Awoki's policy before parsing or embedding. Explicit sensitive data files (`.env`, private keys, credential stores), unsafe symlinks, raw captures, databases/binaries, large text, and generated/vendor text remain out of structural/vector indexing. Coverage-first lexical search is intentionally broader and can account for/search textual lexical-only material locally without sending it to an embedding endpoint.

Supported source is parsed into logical units such as functions, classes,
methods, interfaces, and modules. Awoki uses pinned bundled Tree-sitter grammars.

The parser runtime is pinned as one compatibility matrix rather than relying on
transitive minimum versions:

```text
tree-sitter-language-pack 0.10.0
tree-sitter 0.25.2
tree-sitter-c-sharp 0.23.1
tree-sitter-embedded-template 0.25.0
tree-sitter-yaml 0.7.2
```

The image build loads and exercises every curated grammar. Version resolution
alone is not treated as proof of grammar ABI compatibility. The parser report
includes the resolved distribution versions and supported Tree-sitter language
ABI range, and Docker prints the report directly when the gate fails.
Python has a deterministic standard-library AST fallback. Other unsupported or
unparseable source uses bounded deterministic text chunks and is marked as
fallback.

The derived project database is:

```text
workspace/projects/<project>/index/sqlite/awoki_code.sqlite
```

It stores files, symbols, chunks, definitions, references, resolved/ambiguous
call edges, branch membership, and freshness state. Repository chunks are not
written into the general project-memory SQLite database.

The dedicated Qdrant collection stores one vector for each:

```text
embedding profile + exact chunk content hash
```

A vector can have multiple project, branch, path, and symbol memberships.
Unchanged code moved between files or shared between branches can therefore
reuse the vector without another embedding request.

## Branch behavior

Default search is restricted to the active worktree branch. Index membership is
separate from vector identity, so branch switching can reuse unchanged content
without returning results from the wrong branch.

Every hit includes branch and commit identity. Detached heads and non-Git
repositories receive explicit deterministic scope identifiers.

## Result views

`peek` returns locations and symbols with almost no source.

`context` is the default and returns bounded structural evidence.

`full` returns complete selected symbol chunks under hard per-symbol and total
character limits. It does not dump arbitrary whole repositories.

`diagnostics` is the retrieval-observability view. It omits source previews and
serializes global backend/reranker/refinement telemetry and `stage_top` before
hit data. The complete bounded ranking pool is encoded as `columns+rows` but
stored behind a short-lived project-scoped trace handle instead of being inlined.
The first response contains compact final-hit records, a compact summary of every
candidate actually selected for reranking, and optional complete records for
`diagnostic_targets`. Use `code_diagnostics_trace(trace_id=..., offset=...,
limit=...)` to page the full pool, or provide `target=` to match a candidate path
or symbol directly. Target matching first honors exact/path substring identity,
then uses language-neutral terminal-member plus owner canonicalization so common
receiver/dotted/namespace/Smali spellings can resolve to parser-native qualified
names without a Go-only alias table. The trace includes focus/structural lane eligibility,
admission signals/order, selection, explicit exclusion reasons, reranker
scores/ranks, and final ranks. Traces contain no source previews, are TTL/count bounded in process memory, are project-scoped on read, and disappear when the Awoki MCP process restarts.
This avoids both transport truncation and client-side tool-output scraping while
preserving full-ham internal observability.

Use `code_source_window` on the strongest indexed result before asserting
framework behavior, configuration, or implementation details.

## Call graph

The graph records syntactic calls and resolves them conservatively:

1. `self.method()` / `this.method()` within the containing type;
2. a unique unqualified same-file symbol;
3. an explicitly imported module or symbol alias;
4. a unique unqualified active-branch symbol;
5. otherwise ambiguous or unresolved.

Awoki does not resolve `receiver.method()` merely because a matching method name
exists. The receiver may be a parameter, injected object, dynamically selected
implementation, or runtime proxy. Module-scope calls are represented by explicit
`module:<path>` symbols so entrypoint flow outside functions can still be traced.

Edges carry resolution status and conservative confidence. “Resolved” means
that the structural index found one syntactic candidate under its documented
rules; it is not a proof of Python name binding or runtime dispatch. `code_path`
uses bounded breadth-first search over those resolved edges to trace possible
static function flow. It is not a runtime execution recording and cannot
silently resolve reflection, dynamic dispatch, dependency injection, generated
code, monkey-patching, or same-name shadowing.

## Deterministic claim validation

The user-facing command accepts both atomic claims and broader source-logic
verification requests:

```text
/code-validate-claim handler calls validate_claims
/code-validate-claim validate_claims raises ValueError('invalid issuer') when payload.get('iss') != EXPECTED_ISSUER
/code-validate-claim validate the OAAF tree execution from file parsing through every yes/no terminal outcome
```

`/code-validate-claim` is the single canonical slash command, but it is an
orchestrator rather than a raw grammar wrapper. For a broad request it first uses
repository search only to locate candidate code, resolves exact symbols, opens the
current source, and decomposes the requested behavior into bounded atomic proof
obligations. It then invokes the strict `code_validate_claim` MCP primitive for
each supported obligation. A vague broad request must not be sent directly to the
strict primitive merely to obtain `INCONCLUSIVE`.

The strict MCP primitive intentionally disables semantic mechanisms. It uses only:

- current source/index hashes;
- exact symbol definitions;
- a fresh reparse of the exact source bytes;
- Python lexical-scope checks for parameters, locals, imports, nonlocals, and
  explicit module rebinding;
- structural call edges and bounded graph paths as candidate evidence only;
- edge-by-edge proof for a `can reach` verdict;
- exact Python AST condition and outcome obligations;
- exact active-branch path comparison.

It does not use embeddings, Qdrant similarity, reranking, or synonym guesses.
Semantic retrieval may be used by the outer command to discover where to look,
but it never contributes to a verification verdict. Unsupported or ambiguous
obligations return `INCONCLUSIVE` or `AMBIGUOUS_SYMBOL`; source races return
`STALE_SOURCE`. Absence of a static edge is not treated as a sound runtime
refutation.

Supported atomic forms initially include:

```text
X calls Y
X is called by Y
X can reach Y
X is defined in path/to/file
X raises|returns|rejects EXACT_OUTCOME when EXACT_CONDITION
```

For a broad decision-tree or file-processing request, the command should produce a
set of those exact obligations, verify each one, and reconstruct the result with
per-branch verdicts. The broad request is fully verified only when every required
obligation is `VERIFIED`; one required `REFUTED` obligation refutes the
corresponding assertion, and any required unresolved boundary keeps the overall
result `INCONCLUSIVE`.

For direct calls and paths, `VERIFIED` is currently limited to a strict Python
source profile: each call must be a direct bare-name call that can be tied to the
indexed definition without imports, decorators, shadowing, explicit rebinding,
or cross-file binding assumptions. Other languages and less certain Python
constructs return `INCONCLUSIVE` even when the navigation graph shows a possible
edge.

A `VERIFIED` result is scoped to the hashed source, branch, and supported proof
obligation. It is not automatically a claim about a different build, deployed
artifact, runtime configuration, external service, or whether production
execution reaches the first function.

## Degraded operation

If Qdrant or the embedding endpoint is unavailable, exact structural lookup and
SQLite FTS continue to work. The result reports lexical current/vector stale or
degraded state. Reranker failure preserves deterministic fused ranking.

Parser failure never widens file eligibility. It only changes a safe file from
structural parsing to an explicitly marked fallback. If a parser emits the same
reference occurrence more than once, the SQLite storage boundary deterministically
deduplicates identical emissions before insertion and records that diagnostic. If
the same parser `reference_id` describes conflicting persisted content, indexing
refuses the occurrence instead of using `INSERT OR IGNORE` or silently choosing
one row. If repository bytes change after the eligibility scan, Awoki discards
that attempt and retries policy checks; it never parses or embeds bytes that differ
from the validated content hash.
