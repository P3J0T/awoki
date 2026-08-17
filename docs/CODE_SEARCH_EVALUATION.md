# Code-search golden evaluation

Golden suites live under:

```text
.harness/evaluation/code_search/suites/*.jsonl
```

Run one with:

```text
/code-eval smoke
/code-eval graph
/code-eval security
/code-eval cross-project
/code-eval branches
```

or call `code_evaluate(suite="smoke")`.

## Bundled suites

The release contains several separate suites rather than one tiny all-purpose
fixture:

```text
smoke          definitions, conceptual retrieval, callers, cross-project lookup, abstention
graph          callers, callees, bounded paths, and a deterministic no-path case
security       forbidden environment, raw HTTP, key, and certificate leakage checks
cross-project  explicit multi-project scope and out-of-scope abstention
branches       real Git branch switching, deletion, and wrong-branch leakage checks
multilingual   required Tree-sitter extraction for Python, TypeScript, and Go
live-qdrant    real embedding/Qdrant contribution and cleanup
```

The first five suites are hermetic offline release gates. `multilingual` requires
the pinned parser package actually installed. `live-qdrant` requires the intended
embedding endpoint and Qdrant runtime.

## Isolated fixtures

A suite may start with a `type: fixture` record. Awoki materializes those
repositories under an isolated temporary root, runs the suite, and removes the
temporary workspace. It does not read or modify projects in the user's normal
workspace.

Fixture records are constrained:

- one fixture record, first in the suite;
- normalized project IDs;
- relative repository paths only;
- no `.git` paths or parent traversal;
- bounded project, file, and byte counts;
- UTF-8 text files only;
- explicit opt-in for live Qdrant;
- safe Git branch names for branch scenarios.

A simple fixture:

```json
{"type":"fixture","projects":{"eval-webhook":{"files":{"src/worker.py":"def should_process_delivery(delivery_id):\n    return True\n",".env":"SECRET=excluded\n"}}}}
```

A branch fixture:

```json
{"type":"fixture","projects":{"eval-branches":{"initial_branch":"main","branches":{"main":{"files":{"src/feature.py":"def main_only():\n    return True\n"}},"feature/strict":{"files":{"src/feature.py":"def feature_only():\n    return True\n"}}}}}}
```

A query may switch that isolated repository before searching:

```json
{"id":"feature-definition","projects":["eval-branches"],"checkout_branch":"feature/strict","query":"Where is feature_only defined?","mode":"definition","expected_branch":"branch:feature/strict","expected":[{"path":"src/feature.py","symbol":"feature_only","grade":3}]}
```

This leaves prior branch rows in the derived index, so the suite can detect an
actual branch-filtering failure rather than merely checking a branch label.

## Tree-sitter-required fixtures

A fixture can require structural parsing for named languages:

```json
{"type":"fixture","require_tree_sitter_languages":["python","typescript","go"],"projects":{...}}
```

The evaluator reads the dedicated SQLite code index and fails before scoring if
any required language was handled by fallback parsing. This prevents a
multilingual suite from reporting success merely because lexical fallback found
a symbol.

## Live-Qdrant fixtures

Live vector evaluation must be explicit at both fixture and query levels:

```json
{"type":"fixture","include_qdrant":true,"projects":{...}}
{"id":"semantic","projects":["eval-vector"],"query":"How are repeated callbacks suppressed?","mode":"conceptual","include_qdrant":true,"required_backends":["code_qdrant"],"expected":[...]}
```

The fixture fails immediately if vector indexing degrades. `required_backends`
ensures Qdrant actually contributed to returned evidence rather than merely being
reachable while lexical search did all the work.

All Qdrant memberships created by an isolated fixture are removed in a mandatory
`finally` cleanup. Cleanup failure fails the suite; temporary evaluation points
must not accumulate in the production code collection.

An isolated query cannot request Qdrant unless the fixture itself declared
`include_qdrant=true`.

## Query records

Query records may declare:

- explicit projects or explicit `all_indexed` scope;
- query, mode, view, and limit;
- expected graded project/path/symbol/language/parse-mode/branch matches;
- expected deterministic router mode;
- forbidden paths;
- expected no-answer behavior;
- expected active branch;
- required retrieval backends;
- optional branch checkout for Git fixtures.

Example:

```json
{"id":"webhook-definition","projects":["eval-webhook"],"query":"Where is should_process_delivery defined?","mode":"definition","expected_mode":"definition","expected":[{"path":"src/worker.py","symbol":"should_process_delivery","grade":3}],"forbidden_paths":[".env"],"include_qdrant":false}
```

Suites without a fixture operate on explicitly named installed projects. They are
not isolated and must name every project intentionally.

## Metrics and acceptance

The deterministic JSON report includes:

- Hit@1, Hit@3, Hit@5;
- MRR@10;
- nDCG@10;
- no-answer accuracy, precision, and recall;
- forbidden-path leakage count;
- cross-branch leakage count;
- router mismatch count;
- required-backend failure count;
- p50 and p95 query latency;
- vector creation/reuse counters exposed by search refreshes;
- per-query top results and grades;
- suite SHA-256, parser profile, engine version, and fixture parse modes.

Each report also contains an `acceptance.passed` value. It is true only when:

- every query completed with `status=ok`;
- every positive query returned graded evidence;
- every no-answer query abstained;
- no forbidden path or wrong branch leaked;
- every explicitly required backend contributed;
- every explicitly expected router mode matched.

Ranking metrics are calculated only for records with graded relevant results.
No-answer records are measured separately.

Evaluation reports are derived data under:

```text
.harness/evaluation/code_search/reports/
```

A retrieval metric establishes ranking behavior for the versioned suite. It does
not prove that an agent opened the source correctly or that a program behaves as
claimed. Use `code_validate_claim`, exact source inspection, and executable tests
for behavioral assurance.

The release test suite separately covers deterministic-investigation robustness
that is not a ranking metric: bounded `code_flow_graph` traversal, active-branch
hash checking and giant-line clipping in `code_source_window`, stale-source
rejection, session propagation for the internal MCP primitives, and exhaustive
`code_text_search`/CLI fallback behavior. Fallback tests verify complete match
counting across pagination, multiple roots, default-vs-forensic Git-ignore scope, snapshot-bound stale cursors (including ignored-file changes), policy
exclusions, security-vocabulary coverage canaries, and multi-megabyte single lines whose returned previews remain
bounded. Redaction canaries validate best-effort masking at declared derived/output boundaries; they never authorize dropping evidence merely because a sanitizer does not recognize a value. These regressions prevent repository content from turning a valid
investigation into either an oversized raw-grep transport failure or silent
candidate loss.

## Validation layers

`make validate` is intentionally dependency-tolerant so a developer workstation without host ripgrep or Tree-sitter can still run hermetic/unit regressions. Tests that validate scanner-independent contracts simulate scanner records rather than silently becoming host dependency tests. Any unavailable optional runtime check is reported visibly.

`make validate-runtime` is the stronger environment gate. It requires real `rg`, the prebuilt Go semantics helper or fixed-source local Go fallback, the pinned Tree-sitter runtime, and runs the runtime code-search suites including live Qdrant contribution/cleanup. A release claim must state which gate was actually observed; passing host validation is not evidence that unavailable runtime dependencies were exercised.

## Historical baseline

The pre-structural implementation is not retained as a runtime backend. Its
observed results are captured once from the parent commit in a frozen baseline
artifact under:

```text
.harness/evaluation/code_search/baselines/pre-structural-98f5431.json
```

The frozen observation records the exact fixture hash, parent commit, environment, ranked hits, and its known no-answer false positive. It is evidence for historical comparison, not a second executable backend. Production therefore has one search engine and one freshness model.
