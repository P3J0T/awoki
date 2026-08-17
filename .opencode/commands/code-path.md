---
description: Trace a bounded statically resolved path between two code symbols
---

Use the `project-continuity` skill.

Interpret `$ARGUMENTS` as `SOURCE -> TARGET`, then call
`code_path(source=SOURCE, target=TARGET)`.

Report the exact symbol path, edge confidence, branch and commit identity, and
analysis limits. A static path is possible flow, not proof that runtime executed it.
