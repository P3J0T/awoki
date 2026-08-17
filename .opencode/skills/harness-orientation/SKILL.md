---
name: harness-orientation
description: Use at the start of a task, after compaction, or whenever harness state or scope is unclear.
compatibility: opencode
metadata:
  scope: project
  version: "1"
  tags: harness,orientation,bootstrap,memory
---

# Harness Orientation

## Purpose

Orient inside Awoki without loading unnecessary context.

## Procedure

1. Call `harness_status`.
2. If the active project or paths are unclear, call `load_manifest`.
3. State the active project, runtime mode, memory scopes, and safety policy. If repository analysis is relevant and code indexing is already enabled, include the concise repository assurance from project/code status; do not run a deep verify just for orientation.
4. Before task execution, call `recall_context` with the user's task.
5. If the task implies a procedure, call `search_skills` and load the best matching skill.

## Output

Return:

- active project,
- relevant memory scopes,
- selected skill if any,
- known constraints,
- next action.

## Rules

Project memory overrides global memory. Skills are procedures; memory/RAG is knowledge. Secrets are references only.
