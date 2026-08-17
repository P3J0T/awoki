# Awoki command interface

Awoki is designed for natural-language operation. Slash commands are stable intent anchors, not one wrapper per MCP tool or Python subcommand.

The normal pattern is:

```text
say what you want naturally
→ command or agent routes to the correct MCP tool
→ explicit commands remain for precision or meaningful side effects
```

## Primary commands

```text
/project <natural-language project action>
/codebase <natural-language repository question>
/burp <natural-language Burp action or question>
/recall <project-memory question>
```

Examples:

```text
/project open billing-api
/project add repo oathkeeper
/project list repos
/project fully refresh this project with code and Qdrant
/project pause and write a concise handoff
/project prime oathkeeper for full retrieval
/project remember that staging uses a separate issuer
/project save this investigation result as a finding with the source lines

/codebase how is a view return value converted into a response?
/codebase show locations only for issuer validation
/codebase show the full implementation of token verification
/codebase trace how HTTP input is parsed, validated, transformed, and persisted

/burp find the latest login request
/burp summarize everything observed for api.example.com
/burp save this as a project observation
```


### Repository management and index readiness

`/project` also owns repository membership. Natural requests such as `add repo
oathkeeper`, `list repos`, `remove repo hydra`, or `make oathkeeper the default
repo` route to the `project_repo_*` MCP tools. With only a repository name, Awoki
infers `repo/<name>` and verifies an existing Git checkout is the exact Git root;
it never clones or deletes repository files.

After `project_open` or `project_repo_add`, inspect `repository_index_advice`. If an
existing local structural/FTS snapshot is stale, use the recommended detached
`code_index_refresh_start` first; report its job id and return control. Use
`code_index_refresh_status` only on a later status-dependent turn and cancel only
on explicit request. This local job performs no remote embedding or Qdrant writes.
If structural search is current but semantic vectors are stale/missing, tell the
user conceptual search can fall back locally and offer `code_vector_refresh_start`.
That refresh is also detached; report its job id and return control. Never
autonomously poll it with the model, and cancel only on explicit request. During an
explicit repository-readiness workflow, durable continuation state lets the OpenCode
plugin check local job metadata at the recommended cadence and resume the model only
after a terminal transition. Do not perform remote
embedding merely because a project was opened.

For repeated "prepare/prime/warm this repository" requests, load the
`repository-readiness` skill. It is the reviewed orchestration procedure for
converging on `LOCAL_READY` or `FULL_READY` without adding another slash-command
alias. Full readiness is explicit semantic-materialization intent for the exact
managed scope; ambiguous indexing requests remain local-only until clarified. The
skill keeps a visible OpenCode TODO but stores durable continuation state separately,
so long embedding jobs can survive context compaction/plugin restart. An explicitly
named existing project does not need to be attached for preparation. A different
attached project blocks auto-resume, and true ad-hoc work is never silently converted
into a managed project/vector scope.

### Project capture behavior

`/project` is also the normal save interface. A plain `remember`, `save`, or `note` uses a neutral `observation` record by default. The agent should not turn generic project knowledge into a `finding` merely because it seems important, and it should not demand evidence/confidence fields for ordinary saves. Explicit wording such as `fact`, `finding`, `decision`, `question`, `correction`, or `direction` selects the corresponding semantic kind. High-confidence findings/discoveries are evidence-oriented.

The underlying `save_finding` MCP tool remains available as a compatibility adapter, but it is intentionally not a slash command.

## Precision code commands

These remain separate because they have deterministic semantics that should not be blurred into general search:

```text
/definition <symbol>
/callers <symbol>
/callees <symbol>
/code-path <source> -> <target>
/code-across <explicit projects> <question>
/code-validate-claim <atomic claim or broad source-logic verification request>
/code-index-status
```

`/code-validate-claim` is a natural-language verification front door. If the
request is already one exact supported claim, it calls the strict proof primitive
directly. If the request is broad (for example, "validate this decision tree"),
it first discovers the implementation, decomposes the behavior into exact atomic
claims, and verifies those individually. Semantic retrieval may locate evidence
but never counts as proof.

`/codebase` is not retrieval-only. Repository analysis is evidence-backed by
default: indexed search discovers candidate code, exact symbol operations resolve
the implementation, flow-oriented requests build a bounded relevant structural
graph, and bounded hash-checked source is inspected before behavioral conclusions
are presented. Semantic similarity is discovery only.

Normal `/codebase` use keeps backend details automatic. For deterministic
retrieval experiments, the underlying `codebase_search` MCP tool exposes real
`mode=lexical`, `use_fts`, `use_qdrant`, `use_reranker`,
`structural_promotion`, `result_focus=auto|implementation|balanced|tests|config`,
and `strict_backends`. These controls are diagnostics, not extra slash commands.
Explicit unsupported modes fail rather than silently becoming conceptual search.
`strict_backends=true` means a requested Qdrant/reranker path must actually
succeed. Results expose stage ranks/scores and explicit reranker
requested/attempted/applied telemetry; never infer reranker execution from score
shape.

Conceptual ranking does not globally suppress tests. For implementation/runtime
questions, relevant concrete production implementations receive a bounded
authority preference and duplicate schema/test roles are diversified; explicit
test/config questions promote those roles. Strong coarse production module/file
hits may be structurally refined into contained functions/methods, but those
children must be independently reranked against the original query. Verified
structural edges from a semantic test/config hit may also add production
**candidates**, but graph connectivity is not behavioral proof and promoted
candidates must still earn relevance against the original query. Reranker output
is combined with retrieval by rank rather than raw-score scale.

Two internal MCP primitives support that default workflow without adding more
slash commands:

```text
code_flow_graph      bounded reachable graph from one exact entry point
code_source_window   bounded active-branch source with full-file hash verification + evidence ID
code_evidence_verify verify a prior source evidence ID against current bytes/snapshot
code_semantics_check allow-listed fixed Go language/stdlib helper; never repository code
```

`code_flow_graph` traverses only resolved calls and preserves ambiguous/unresolved
edges as explicit boundaries. `code_source_window` clips giant lines explicitly,
so a source file cannot force an arbitrarily large response. Use the precision
slash commands when you already know the exact operation you want.

The former `/code-peek`, `/code-context`, and `/code-full` wrappers are represented as natural-language modifiers of `/codebase`:

```text
/codebase locations only for JWT validation
/codebase explain the JWT validation path with bounded context
/codebase show complete symbol bodies for JWT validation
```

## Burp side-effect commands

Read-only inspection, searching, summarizing, and saving can use `/burp`. Actions with materially different side effects stay explicit:

```text
/burp-repeater <clearly identified request>
/burp-intruder <clearly identified request>
/burp-send <one clearly identified request>
```

`/burp-intruder` stages a request only; it does not launch an attack. `/burp-send` authorizes one unambiguous network send, not repetition, scanning, or escalation.

Diagnostics:

```text
/burp-status
/burp-validate
/retrieval-status
/project-status
```

## Reliability and delivery

```text
/explore
/verify
/reliability-check
/ship-check
/backup
```

These remain separate because they express different evidence and authorization boundaries.

## Specialized maintenance commands

```text
/harness-boot
/review-promotions
/demote-memory
/lavish
```

Code-search evaluation and bulk index maintenance are maintainer operations, available through Make targets and direct MCP tools rather than normal slash commands:

```bash
make validate              # dependency-tolerant host/hermetic gate
make validate-runtime      # requires real rg + Go + Tree-sitter + runtime code-search/Qdrant gate
make code-search-eval
make code-search-eval-runtime
make index
make index-vector
make runtime-config        # redacted effective SSH/MCP runtime configuration
make embedding-benchmark  # synthetic query + bulk embedding latency
make reranker-benchmark   # synthetic reranker request latency/contract
```

The three runtime-diagnostic targets can be launched from the host checkout or from
inside `awoki-opencode-ssh`. They never require manually sourcing `/run/awoki/runtime.env`.
Their host wrapper is compatible with the macOS system Bash 3.2, and the hermetic host
validation path does not require runtime-only `httpx`/`openai` packages.
The embedding/reranker probes use fixed synthetic text only and use Python's standard-library
HTTP client so the diagnostic itself does not depend on the OpenAI/httpx SDK packages.
Runtime profiles filter child environments but do not sandbox hostile same-user processes;
do not execute untrusted target/tool code in a credential-bearing OpenCode container.
Awoki's passive Git/rg repository subprocesses independently strip provider credentials and
ambient loader/interpreter/SSH-agent overrides before launch.

Natural-language equivalents are also valid, for example:

```text
Refresh all safe memory indexes.
Fully refresh the attached project with code and Qdrant.
Run the structural code-search smoke evaluation.
```

For normal review work, choose exact-search tooling by intent. Use Awoki indexed
search for conceptual/architectural discovery, OpenCode `Grep` for ordinary known
string/symbol lookup, and Awoki `code_exact_search` when full ripgrep-style options make
complex or exhaustive exact enumeration clearer or cheaper without Bash. `code_exact_search`
does not need semantic retrieval to fail first when the task itself is exact enumeration. If
structured exact-search output errors/truncates or cannot establish the coverage needed
for a claim, use the internal `code_text_search` MCP tool. It scans every
policy-eligible file in scope, materializes the exhaustive result once, and serves
later pages from that snapshot instead of rescanning the repository. If one MCP
call reaches its soft operation deadline, continue the returned cursor until
`scan_complete=true`; then continue paging until `search_complete=true`, and
require `repository_universe_complete=true` before claiming complete coverage.
For explicit forensic coverage of Git-ignored untracked files, set
`include_ignored=true`; `.env` and other explicit sensitive data files remain
opaque in returned previews. Lexical results remain discovery until authoritative
source is inspected. Acceptance contracts may intentionally forbid native tools.

If MCP itself is unavailable, use the diagnostic equivalent:

```bash
.harness/bin/code-search-fallback 'ProcessTree|loadTree' \
  workspace/projects/PROJECT/repo/oathkeeper/src \
  workspace/projects/PROJECT/repo/oathkeeper/pkg
```

For a legacy single-repository project, drop the `/oathkeeper` child component.

The helper accepts multiple paths, `--include-ignored`, and an explicit `--cursor`; do not pipe it
through `head`. Lexical results are discovery-only and must not be presented as
deterministic proof.

## Removed redundant aliases

The command surface intentionally does not expose one slash command for every Burp archive helper, project lifecycle tool, or result view. Underlying MCP tools and archive scripts remain available to skills and maintainers.

Examples of replacements:

```text
/project-create NAME        -> /project create NAME
/project-resume NAME        -> /project open NAME
/project-handoff            -> /project pause and write a handoff
/project-index              -> /project fully refresh with code and Qdrant
/index-memory               -> natural-language refresh or Make index targets
/code-peek/context/full     -> natural-language /codebase view request
/code_validate_claim        -> /code-validate-claim
/burp-find-request ...      -> /burp find ...
/burp-host-report ...       -> /burp summarize host ...
/burp-request-to-repeater   -> /burp-repeater ...
/burp-request-to-intruder   -> /burp-intruder ...
/burp-send-request          -> /burp-send ...
```

Do not invent slash-command aliases that are not listed here. MCP tool names remain callable by the agent even when they are not exposed as slash commands.
