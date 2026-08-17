---
description: Inspect the attached project's continuity and structural-code freshness
---

Use the `project-continuity` skill. Call `project_status` for the attached or named project. Also call `code_index_status` when repository indexing, branch state, parser health, graph counts, or code-vector freshness is relevant.

Report the active project, attachment/session, repository branch and commit, concise repository assurance/anomalies when code indexing is enabled, dirty state, generated-view freshness, SQLite/code-index freshness, Qdrant freshness, degradation, and the smallest next action. Do not start a refresh unless the user asks.
