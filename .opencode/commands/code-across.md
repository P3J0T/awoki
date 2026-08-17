---
description: Search repository code across an explicit set of Awoki projects
---

Use the `project-continuity` skill.

Parse `$ARGUMENTS` as an explicit comma-separated project list followed by a
natural-language query, then call `cross_project_code_search` with those projects.
Use `all_indexed=true` only when the user explicitly says all indexed projects.
Never silently widen scope. Label every hit with project, repository, branch,
path, symbol, and freshness.
