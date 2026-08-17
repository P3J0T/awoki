---
name: repository-readiness
description: Prepare or verify one exact managed Awoki repository/source for local or full retrieval using one durable parent preparation job. Long structural/vector phases advance below the model; OpenCode continuation is best-effort UX only.
compatibility: opencode
metadata:
  scope: project
  tags: repository,indexing,fts,qdrant,embedding,reranker,readiness,code-review,background,todo
  workflow: repository-prepare-parent-job
---

# Awoki Repository Readiness

## Purpose

Use this skill when the user asks to **prepare**, **prime**, **index**, **warm**, or
make an existing managed repository/source ready for code review, hybrid retrieval,
or semantic search.

Normal readiness is owned by one detached Awoki parent job:

```text
repository_prepare_start
  -> structural freshness / refresh / verify
  -> semantic-vector freshness / refresh (full mode only)
  -> exact published-membership verification
  -> Qdrant + embedding + reranker probes (full mode only)
  -> LOCAL_READY | FULL_READY | CONFIGURATION_BLOCKED | PRECONDITION_FAILED
```

The model must not orchestrate the structural/vector transitions while the job runs.
The parent job owns them, including long waits. OpenCode TODO and session continuation
are convenience/notification layers, not the source of truth and not a readiness
correctness dependency.

## Readiness modes and consent

`mode="local"` establishes:

```text
LOCAL_READY
  structural/FTS snapshot current
  parser/index provenance current
  no remote embedding required
```

`mode="full"` establishes:

```text
FULL_READY
  LOCAL_READY
  + semantic vector membership current and published for the same source identity
  + Qdrant healthy
  + embedding configuration ready and live probe healthy
  + reranker enabled/configuration ready and live probe healthy
```

Natural requests such as **"prime this repo for full retrieval"**, **"prepare this
repo for semantic code review"**, or **"index everything needed for hybrid search"**
are explicit authorization to materialize semantic vectors for the exact managed
scope. Merely opening/registering a repo/source is never authorization for remote
embedding. Ambiguous **"index this repo"** means `local` unless semantic/full intent
is explicit or the user confirms it.

## Exact managed scope: attached, unattached, and ad-hoc

Readiness belongs to a managed project + exact repo/source identity, not to a chat
session.

- If the intended project is attached, use it.
- If the user explicitly names an existing project/repo while the session is
  unattached, call the preparation tool with explicit `name=`/`repo=`. Do **not**
  attach the project just so a background job can run.
- If a different project is attached, never silently switch it. Explicitly named
  managed scope may still be prepared because the parent job is independent of
  session attachment; any later conversation resume must not hijack the active
  project.
- If there is no existing managed project/source identity, return
  `MANAGED_SCOPE_REQUIRED`. Do not silently create a project or persist vectors for
  an arbitrary path.
- Ephemeral/ad-hoc EvidenceSource preparation is a separate future lifecycle; until
  that exists, durable preparation fails closed rather than weakening managed-scope
  semantics.

## Required safety boundaries

- Use Awoki MCP for readiness/project/index/retrieval operations.
- Require an existing managed project and one exact registered repo/source.
- Never clone, pull, checkout, move, delete, execute, or otherwise mutate repository
  contents as part of readiness.
- Never source or print `/run/awoki/runtime.env`. Never print API keys or expose secret-bearing values.
- Structural indexing is local/credential-free.
- Full semantic materialization is remote-upload-capable and requires explicit full
  intent for the exact managed source.
- Full mode must stop before semantic upload if passive embedding/reranker
  configuration is missing, disabled, or contradictory.
- Never publish incomplete vector membership as `FULL_READY`.
- Never use partial vectors as ordinary semantic readiness. A future explicit
  degraded-semantic mode may exist, but it is not `FULL_READY`.
- Do not model-poll long jobs. Do not manually chain structural -> vector -> verify
  when the parent job is active.
- Transient HTTP/transport retry is owned by the vector worker and is bounded.
  Failed whole-job restart remains an explicit decision; never loop failed jobs.
- Cancellation is explicit-user-only through `repository_prepare_cancel`; cancelling
  optional session continuation does not imply cancelling the parent worker.

## Normal workflow

### 1. Resolve intent and exact scope

Resolve `name=` and one of `repo=`/`source_id=` from the user's request, current
managed attachment, or the sole registered managed source. If ambiguous, ask one
concise scope question. Do not create or attach a project merely to satisfy this
skill.

Choose:

```text
mode="local"  # structural/FTS only
mode="full"   # explicitly authorized semantic/hybrid readiness
```

For full mode, the parent start performs a passive configuration gate before any
semantic upload. Do not shell-export around a `CONFIGURATION_BLOCKED` result.

### 2. Start or adopt one parent job

Call exactly one normal orchestration tool:

```text
repository_prepare_start(
  name=<existing project>,
  repo=<exact repo>,          # or source_id=<exact source>
  mode="local" | "full",
  resume_goal=<requested work after readiness, if any>
)
```

The call is idempotent for an active identical scope/mode. `already_running` means
adopt the returned parent job; do not start another.

The parent job may adopt existing structural/vector child jobs for that exact scope.
It owns child polling, structural verification, vector verification, and final backend
probes without LLM turns between phases.

### 3. Project TODO = visible projection only

For a non-terminal parent job, maintain a compact OpenCode todo using `todowrite`, for
example:

```text
[done] resolve managed repository scope
[in progress] prepare full retrieval for oathkeeper (background)
[pending] resume retrieval-precision work after FULL_READY
```

If returned parent progress exposes a child phase, a more detailed projection is fine,
but never claim a completed phase based solely on TODO. Parent MCP status is
canonical.

On a blocked vector phase, reflect the truth rather than leaving a generic running
marker, e.g.:

```text
[blocked] materialize semantic vectors — transient failures exhausted; persisted
          content-addressed vectors remain reusable
```

### 4. Optional best-effort OpenCode continuation

If the user explicitly asked to continue some work after readiness and an interactive
OpenCode session is available, the skill may schedule one session continuation that
waits on the **parent** job:

```text
project_continuation_schedule(
  workflow="repository-readiness",
  phase="repository_prepare_wait",
  wait_tool="repository_prepare_status",
  wait_job_id=<rpr_...>,
  wait_seconds=<recommended_poll_after_seconds>,
  name=<project>,
  repo=<repo when applicable>,
  source_id=<source when applicable>,
  next_action="inspect the parent repository preparation terminal outcome once",
  resume_goal=<requested follow-on work>,
  auto_resume=true
)
```

Use `project_continuation_status` only when inspecting that optional session bridge,
and `project_continuation_finalize` after a successfully resumed terminal handoff. The
parent `repository_prepare_status` remains canonical. Then return `PREPARATION_RUNNING`.

Important: this continuation is **best effort** and is not a correctness dependency for
repository readiness. OpenCode may not reliably re-enter an old idle assistant conversation in
every runtime/lifecycle state. Failure to wake the conversation does not affect
repository preparation. The parent continues to a terminal result independently. On
the user's next interaction, inspect the parent status and resume the stored goal if
appropriate.

Do not use TUI prompt-buffer submission as a correctness mechanism and do not make
same-session wakeup a readiness invariant.

### 5. Later status / resumed interaction

When the user asks for status, or a best-effort continuation wakes successfully, call:

```text
repository_prepare_status(name=..., job_id=<rpr_...>)
```

once. Do not manually poll child jobs unless explicitly debugging parent orchestration.

Interpret terminal outcomes:

- `FULL_READY`: full hybrid readiness is established. Mark readiness TODO complete
  and continue saved/requested follow-on work using the exact scope.
- `LOCAL_READY`: structural/FTS readiness is established; semantic readiness was not
  requested.
- `CONFIGURATION_BLOCKED`: report redacted configuration classes and operator action;
  do not modify `.env` from the skill.
- `PRECONDITION_FAILED`: report exact provenance/membership/backend/child-job blocker.
  If a vector child failed after persisting vectors, state that content-addressed
  vectors remain reusable. Do not automatically restart the failed parent/child job.
- `cancelled`/`interrupted`: report exact state and stop.

If a parent failed from transient embedding requests after bounded worker-level retries
were exhausted, an explicit later `repository_prepare_start` for the same unchanged
source may reuse already-persisted vectors and process remaining chunks. That is a new
explicit preparation attempt, not an automatic infinite retry.

## Direct lower-level tools are escape hatches

`code_index_refresh_start/status/cancel` and
`code_vector_refresh_start/status/cancel` remain available for operator debugging,
benchmarks, and precise maintenance. They are **not** the normal implementation of a
prime/prepare request once `repository_prepare_*` is available.

Do not recreate the old model-driven sequence by calling each lower-level tool in
turn.

## Output contract

### `PREPARATION_RUNNING`

Report compactly:

- project + exact repo/source
- parent job id (`rpr_...`)
- mode (`local`/`full`)
- current parent phase + bounded child progress if present
- recommended status interval
- continuation id if best-effort session continuation was scheduled
- explicitly state that the parent advances independently of model/chat polling

Do **not** promise that the same OpenCode assistant turn will wake 100% reliably.

### `FULL_READY`

Report project/scope identity, source revision/content identity/assurance, structural
freshness, exact published vector membership/collection, redacted embedding/reranker
identity/settings, and live backend probe health.

### `LOCAL_READY`

State that structural/FTS/exact/structural review is ready and semantic/vector/rerank
readiness was intentionally not established.

### `CONFIGURATION_BLOCKED`

Report only non-secret configuration facts and operator action:

```text
update host .env -> recreate awoki-opencode-ssh -> start a fresh OpenCode process
```

### `MANAGED_SCOPE_REQUIRED`

State that durable readiness requires an existing exact managed project/source. Ask
whether the user wants to create/register one; never do it implicitly.

### `PRECONDITION_FAILED`

Use for failed child jobs, stale/unverifiable provenance, incomplete/mismatched
published membership, or backend/protocol failure after bounded lower-level retry.

## Idempotence and reliability invariant

Repeated preparation converges on verification rather than rebuilding healthy state.
The invariant is:

```text
repository/source preparation can reach and persist a truthful terminal readiness
result even if OpenCode never produces another assistant turn.
```

Conversation resumption is an optional UX enhancement on top of that invariant.
