---
description: Recall scoped project or project-plus-global context for a query
---

For a named or attached project, prefer `project_search(query=$ARGUMENTS)`.
Use `recall_context(query=$ARGUMENTS)` only when mixed project/global recall and
matching skills are explicitly useful. Project-local results override labeled
global results. Call MCP tools directly; they are not Bash commands.
