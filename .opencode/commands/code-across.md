---
description: Search repository code across an explicit set of Awoki projects
---

Use the `project-continuity` skill.

Parse `$ARGUMENTS` as an explicit comma-separated project list followed by a
natural-language query. Require 1–8 exact project IDs and native user approval,
then call `cross_project_code_search` with those projects. `all_indexed=true` is
rejected; ask the user to name a bounded scope instead of enumerating projects.
For multiple repositories inside one project, use `codebase_search` instead.
Never silently widen scope. Label every hit with project, repository, branch,
path, symbol, and freshness.
