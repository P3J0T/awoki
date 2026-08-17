---
description: Open, inspect, refresh, search, save, or pause an Awoki project using natural language
---

Use the `project-continuity` skill and interpret `$ARGUMENTS` as the user's project intent.

Route naturally:

- create, open, resume, or switch -> `project_open`
- add/register a repository -> `project_repo_add`; infer `repo/<repo-id>` when the user names only the repository
- list repositories -> `project_repo_list`
- remove a repository registration -> `project_repo_remove` (never delete the checkout)
- choose the default repository -> `project_repo_default`
- status or freshness -> `project_status` and, when code health is relevant, `code_index_status`
- prepare/prime/warm a managed repository for code review or full hybrid retrieval -> load the `repository-readiness` skill; start/adopt one durable `repository_prepare_*` parent job and distinguish local-ready from explicit full-ready semantic materialization
- refresh views or safe memory indexes -> `project_refresh`
- "fully refresh project memory/index views" -> use `project_refresh` with explicit `include_code` / `include_qdrant` flags when the user actually requests those broader project indexes
- "refresh/rebuild the local code index", "refresh structural index", or "reindex code" -> `code_index_refresh_start`; report the job id and return control; use `code_index_refresh_status` only on a later status request or when another requested action requires fresh state
- "refresh code vectors", "vector refresh", or "materialize Qdrant for code" -> `code_vector_refresh_start`; report the job id and return control; use `code_vector_refresh_status` only on a later status request or when another requested action requires fresh state
- save/remember/note something -> `project_capture`; default to neutral `kind="observation"`, and use a stronger kind only when the user explicitly states or clearly implies it
- recall prior project knowledge -> `project_search`
- pause or hand off the project/session -> `project_pause`
- checkpoint/status/finalize one long-running generic task -> `project_task_checkpoint` / `project_task_status` / `project_task_finalize`
- inspect/cancel optional conversation continuation -> `project_continuation_status` / `project_continuation_cancel`; repository-readiness may schedule it only as best-effort UX after the durable parent preparation job starts

When no project is attached and the requested project name cannot be inferred, ask one concise question. Do not invent shell commands such as `awoki_project_refresh`, and do not use a transient Bash `export` to reconfigure an already-running MCP process.

Natural-language examples:

```text
/project open payment-api
/project add repo oathkeeper
/project add repo hydra from repo/hydra
/project fully refresh this project with code and Qdrant
/project remember that staging uses a separate issuer
/project save this investigation result as a high-confidence finding with the file and line evidence
/project what did we conclude about token replay?
/project pause and write a concise handoff
```

When repository readiness is requested, let the `repository-readiness` skill own the
full lifecycle through one `repository_prepare_start` parent job. The parent owns
structural refresh, vector materialization, membership verification, and backend probes
without model polling. OpenCode TODO is only a visible projection. Optional
`project_continuation_schedule` can best-effort resume the original conversation after
the parent reaches a terminal state, but it is not required for readiness correctness.
An explicitly named existing managed project can be prepared without attaching it to
the current session; if another project is attached, optional continuation must not
auto-switch it. True ad-hoc paths are `MANAGED_SCOPE_REQUIRED`, not silently promoted.
Cancellation of workers remains explicit-user-only. Never start remote embedding/Qdrant
materialization merely because a project was opened.
