---
name: scoped-memory
description: Save, classify, recall, promote, or reject local and global memories safely, including explicitly requested sensitive no-RAG records.
compatibility: opencode
metadata:
  scope: project
  version: "2"
  tags: memory,rag,scope,global,project
---

# Scoped Memory

## Purpose

Manage local project knowledge and reusable global knowledge without cross-project leakage.

## Scope Rules

- Start project-local.
- Promote only generalized, safe lessons.
- Never automatically promote secrets or private case material. If the user explicitly asks to save sensitive plaintext globally, use the explicit sensitive no-RAG path rather than ordinary promotion.
- Project memory shadows global memory.

## Save Procedure

1. Call `classify_memory` if scope is unclear.
2. Use `save_project_fact` for local facts.
3. Use `propose_promotion` for possible reusable lessons.
4. Use `save_global_fact` only when explicitly requested or after review.

## Recall Procedure

1. Use `recall_context` for normal project-aware recall.
2. Use `search_rag` when you need explicit retrieval details or scope control.
3. Use `index_all` or `index_project` before large artifact recall. For repository
   questions prefer `codebase_search` or `/codebase`, which explicitly enables
   and refreshes the safe source index.
4. Read project hits first.
5. Use global hits as defaults only when project memory does not override them.
6. Report confidence and source when relevant.

## Indexing Procedure

- Memory and skill FTS are refreshed automatically during recall.
- For evidence corpora, call `index_project(include_artifacts=true, include_code=false, include_qdrant=true)`.
- For source-heavy work, prefer `/codebase`; direct `include_code=true` remains
  available for explicit bulk indexing workflows.
- `project_search` can search the broader safe project set after code is indexed,
  but continuity reconciliation must remain memory-only.
- Qdrant is the semantic store but is derived and may be temporarily unavailable.
  SQLite FTS and JSONL fallback still work in degraded mode.
