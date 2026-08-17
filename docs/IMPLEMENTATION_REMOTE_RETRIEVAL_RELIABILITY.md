# Remote Retrieval, Sensitive Memory, and Reliability Implementation

This checklist records the implementation agreed for Awoki.

## Retrieval

- [x] Keep Qdrant as a core service and preserve hybrid FTS/vector retrieval.
- [x] Remove local embedding and reranker model dependencies and caches.
- [x] Default to an OpenAI-compatible remote embedding endpoint.
- [x] Configure the current TEI/Jina code model as `text-embeddings-inference`
      with 768-dimensional vectors and a model-specific Qdrant collection.
- [x] Keep continuity operations usable when semantic retrieval is temporarily unavailable.
- [x] Add an optional remote HTTP reranker with fail-open ordering fallback.
- [x] Add native TEI rerank request support (`query`, `texts`, `raw_scores`).
- [x] Add `/codebase` and `codebase_search` for repository-only exact, FTS,
      Qdrant, and optional reranked retrieval.
- [x] Keep memory reconciliation restricted to continuity records even after
      repository indexing is enabled.
- [x] Expose retrieval configuration and degraded state through diagnostics.

## Continuity

- [x] Reconcile new project captures against similar project memory.
- [x] Preserve the append-only journal.
- [x] Automatically deduplicate exact captures and attach reconciliation metadata.
- [x] Preserve ambiguous contradictions rather than silently superseding them.
- [x] Support explicit sensitive project/global memory writes with `no_rag` indexing.

## Credentials

- [x] Remove the built-in credential module and credential MCP tools.
- [x] Remove credential commands, skill, initialization paths, tests, and documentation.
- [x] Do not prohibit explicit sensitive memory storage or retrieval.
- [x] Keep sensitive values outside Qdrant, FTS, generated views, and automatic recall.

## Reliability

- [x] Add `/explore`, `/verify`, `/reliability-check`, and `/ship-check` commands.
- [x] Add an on-demand reliability skill.
- [x] Add concise always-loaded fallibility and evidence rules.
- [x] Load reliability invariants through `AGENTS.md` and OpenCode instructions, and preserve a bounded reminder during compaction without appending extra system messages.
- [x] Record executable checks and prevent failed checks from being represented as passed.
- [x] Keep Burp behavior unchanged; route Burp-derived verification through the existing Burp skill.

## Workbench and container boundary

- [x] Add minimal Neovim plus a vendored, integrity-recorded gpakosz/Oh my tmux! configuration and runtime validation to the SSH image.
- [x] Remove passwordless sudo from the normal OpenCode user.
- [x] Bake Awoki source into the image and mount only explicit runtime trust domains.
- [x] Put Qdrant alone on an internal data network; give Awoki/OpenCode a separate egress network for remote embeddings and host Burp MCP.
- [x] Preserve safe access to host Burp MCP through `host.docker.internal`.
- [x] Add an ad-hoc `/lavish` workflow in the SSH container plus a macOS localhost opener; no ambient hook or external share.
- [x] Keep no-mistakes optional and invoked only by `/ship-check` when installed/configured.

## Release gates

- [x] Unit tests pass.
- [x] Harness validation passes.
- [x] Shell and TypeScript checks pass.
- [x] Clean-clone initialization passes.
- [x] Compose YAML and repository mount/network contracts validate structurally. Live Docker Compose validation remains environment-dependent.
- [x] Qdrant readiness is checked from the internal Docker network rather than
      through the host loopback endpoint.
- [x] macOS canonical `/var` versus `/private/var` paths are accepted by safe
      repository and artifact containment checks.
- [x] No built-in credential tools remain on the MCP surface.
- [x] No local model packages or model mounts remain.
