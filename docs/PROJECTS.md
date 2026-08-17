# Awoki projects

Awoki projects are continuity-first free workspaces under:

> `workspace/` is runtime-only. Git, Docker builds, and release archives exclude it completely, including any checked-out target repository source code.

```text
workspace/projects/<project_id>/
```

The authoritative design is [CONTINUITY.md](CONTINUITY.md).

## Layout

```text
workspace/projects/<project_id>/
├── project.json
├── README.md
├── SITUATION.md                 # generated short snapshot
├── HANDOFF.md                   # generated bounded resume document
├── notes/
│   └── thoughts.md              # free-form notes
├── memory/
│   ├── continuity.jsonl         # canonical append-only continuity journal
│   ├── facts.jsonl              # migration compatibility
│   ├── findings.jsonl           # migration compatibility
│   ├── hypotheses.jsonl         # migration compatibility
│   ├── decisions.jsonl          # migration compatibility
│   ├── events.jsonl             # migration compatibility
│   └── pending.jsonl            # optional compatibility facet
├── repo/                       # legacy Git root OR container for registered child repositories
├── corpora/
├── artifacts/
├── reports/
├── scratch/
└── index/
    ├── safe_artifacts.jsonl
    ├── sqlite/
    └── manifests/
```

A project may contain research, notes, partial analysis, repositories, Burp
observations, documents, questions, or decisions. It does not require a goal or
pending task.

### Repository registry

Legacy projects keep one exact Git/filesystem root at `repo/`. Multi-repository
projects register exact child roots beneath that directory:

```text
repo/
├── oathkeeper/.git/
├── hydra/.git/
└── keto/.git/
```

Use natural language (`/project add repo oathkeeper`) or the MCP
`project_repo_add` tool; an omitted path infers `repo/oathkeeper`. Registration
never clones, moves, or deletes the checkout. `project_repo_remove` removes only
the registration. `project_repo_default` chooses the preferred child for
otherwise-unambiguous operations. Broad code discovery spans all enabled
registered repositories, while exact operations require `repo=` when necessary.

## Natural language

```text
Create project asd.
Resume asd and inspect the repositories.
Add repo oathkeeper.
Remember that staging uses another issuer.
This previous finding was wrong; replace it with this correction.
Save this report with asd.
What did we establish about OAuth?
/codebase Where is JWT validation implemented?
Pause here.
```

The preferred continuity tools are `project_open`, `project_capture`, `project_search`,
`project_refresh`, `project_pause`, and `project_status`. Repository membership is
managed with `project_repo_add`, `project_repo_list`, `project_repo_remove`, and
`project_repo_default`.

`project_open` is the lightweight default: it returns repo/readiness state, active session work, recent prior-material pointers, and bounded continuation guidance. Use `project_resume` only when the dense SITUATION/HANDOFF continuity projection is explicitly useful; use `project_search` for targeted older knowledge.

Use `/codebase` or `codebase_search` for repository-only questions. It is a
specialized analysis operation rather than a seventh continuity operation.
Repository understanding is evidence-backed by default: indexed retrieval
discovers candidates, exact structural tools resolve symbols/relationships,
`code_flow_graph` scopes relevant reachable flow, and `code_source_window`
provides bounded hash-checked source plus a compact evidence ID before behavioral
conclusions are stated. `code_evidence_verify` can later distinguish changed source
bytes from an unchanged file observed under a different repository snapshot/view.

## Resume order

```text
SITUATION.md
HANDOFF.md
recent reflections
targeted project search
raw artifacts only when explicitly needed
```

The user's current instruction always overrides suggested continuation.

## CLI

```bash
python .harness/project.py create asd
python .harness/project.py repo-add asd oathkeeper repo/oathkeeper --default   # administrative fallback; natural language is preferred
python .harness/project.py repo-list asd
python .harness/project.py resume asd
python .harness/project.py capture asd "Mapped both authentication flows" --kind finding
python .harness/project.py status asd
python .harness/project.py handoff asd
python .harness/project.py pause asd --summary "Authentication review is checkpointed"
.harness/bin/awoki migrate asd --preview
.harness/bin/awoki migrate asd --apply
.harness/bin/awoki sessions --stale-after-hours 24
.harness/bin/awoki sessions --stale-after-hours 24 --apply
.harness/bin/awoki doctor
```

Pending commands remain available for compatibility, but pending items are optional.

## Indexing

Project indexing is fail-closed. Use `project_index_preview` before broad indexing.
Safe generated views, continuity records, notes, reports, selected corpora,
and registered safe summaries may be indexed. Repository code remains disabled
unless both the index request and `project.json` policy explicitly enable it. Raw evidence, credential material,
security vocabulary and secret-like identifiers are not exclusion reasons. High-confidence values are redacted best-effort while analysis remains retrievable. `.env`, key/credential files, HAR/HTTP/raw traffic stay out of semantic/structural indexing; repository-local exhaustive text coverage may account for textual secret/config files with opaque previews. Explicit no-RAG material remains excluded.

The first `/codebase` call is the explicit code-index request. It enables the
project policy for the legacy `repo/` root or all enabled registered child roots.
It establishes the first local structural source index when needed. Existing stale
structural snapshots and explicit full reindex requests use detached
`code_index_refresh_start` rather than blocking an interactive MCP request; a later
`code_index_refresh_status` reports bounded file/parser progress without source
text. Remote semantic-vector materialization remains explicit: `project_open` and
repository registration return `repository_index_advice`, and missing/stale vectors
should be offered as `code_vector_refresh_start` rather than triggered silently.
Both detached jobs return control immediately; do not autonomously poll them. Qdrant
collection materialization is preflighted before expensive embedding begins. Once current, conceptual search can combine exact/FTS, Qdrant `kind=code`
vectors, and optional reranking. The returned chunks are discovery;
semantic similarity is not behavioral proof. For flow questions the agent should
resolve an exact entry point, use a bounded structural flow graph, inspect
hash-checked source for conditions/assignments/arguments/outcomes, and use strict
atomic claim validation selectively where supported. Supported deterministic Go
primitive claims should use `code_semantics_check` rather than remembered arithmetic
or stdlib behavior. For conceptual/architectural questions, use Awoki indexed/
structural discovery first. For a known string/symbol lookup, OpenCode Grep is a
normal exact-search tool; for complex or exhaustive exact enumeration, Awoki
`code_exact_search` provides structured ripgrep power without Bash. Neither lexical
path is behavioral proof, and `code_text_search` remains the machine-visible exhaustive
coverage path when client output is incomplete or the claim requires complete scope.

`project_search` is broader and may include code after that policy has been
enabled. Continuity reconciliation remains memory-only, so source chunks cannot
be classified as duplicate or contradictory prior memories.

### Repository assurance

When code indexing is enabled, refresh/verify materializes repository provenance as
`VERIFIED_SNAPSHOT`, `WORKING_TREE_BOUND`, or `FILESYSTEM_BOUND`. Exact Git root,
HEAD/tree, replace/sparse state, local history limitations, and source-byte evidence
are kept separate from human authorship: Git author/committer fields are metadata
claims, not proof of identity. Dirty, shallow, sparse, submodule, filtered, partial, or
other unusual states lower/disclose assurance; they do not censor source. Passive Git
inspection disables fsmonitor/lazy promisor fetches and does not automatically invoke
configured content filters or signature verifier programs.

A clean shallow clone can still establish the current source snapshot while reporting
that its local ancestry is incomplete. Awoki cannot prove that rewritten, unreachable,
garbage-collected, or remote-only commits never existed without an independent anchor.

## Moving projects between installations

Do not copy only a project SQLite database. `make backup-portable` captures the entire project workspace, canonical continuity, repository, reports, artifacts, and configured global context while excluding stale project/Qdrant indexes. Portable restore rebuilds lexical indexes and leaves repository semantic indexing opt-in through `/codebase`. See `docs/BACKUP_RESTORE.md`.
