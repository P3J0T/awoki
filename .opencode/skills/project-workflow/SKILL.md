---
name: project-workflow
description: Compatibility alias for older Awoki project commands. Prefer the project-continuity skill and six generic continuity tools.
compatibility: opencode
metadata:
  scope: project
  tags: project,compatibility,deprecated
---

# Compatibility Project Workflow

Load `project-continuity` for all new project work.

Legacy `project_create`, `project_resume`, `project_handoff`, `project_note`,
`project_pending`, and `project_mark_pending` remain available, but pending items
are optional and typed memory files are migration compatibility stores.

The canonical model is defined in `docs/CONTINUITY.md`:

```text
free workspace -> memory/continuity.jsonl -> generated SITUATION/HANDOFF
```
