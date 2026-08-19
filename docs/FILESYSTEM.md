# Awoki filesystem layout

Awoki separates tracked tool source from ignored runtime data.

```text
repository root
  Awoki Python, OpenCode configuration, skills, commands, plugins, docs, tests

workspace/
  project repositories, notes, continuity, corpora, reports, artifacts, scratch

.harness runtime mounts/
  state, indexes, managed artifacts, legacy compatibility memory, notes, tmp

.awoki-global/ or /global
  reusable global memory, skills, registry, Burp run summaries, knowledge inbox

data/qdrant/
  Qdrant database; `collections/` is created during initialization for bind-mounted collection storage
```

## Initialize or repair

```bash
./init-awoki.sh
```

This creates missing runtime directories and placeholder files without truncating existing memory, Burp runs, registry data, or projects. Runtime launchers do not silently initialize the base layout.

## Main local runtime directories

```text
workspace/
  projects/<project_id>/
  notes/
  corpora/
  artifacts/
  reports/
  templates/
  scratch/

.harness/
  state/
  index/
  artifacts/
  memory/        # ignored compatibility stores
  notes.md       # ignored local notes
  tmp/

.awoki-global/
  global/
  skills/
  state/burp/runs/
  state/registry/context-packs/
  state/knowledge-inbox/sources/
  state/knowledge-inbox/process-notes/

data/qdrant/
  collections/   # initialized explicitly; Qdrant creates collection children here
```

There is no built-in credential directory or credential database.

## Project workspace

```text
workspace/projects/<project_id>/
├── project.json
├── SITUATION.md
├── HANDOFF.md
├── notes/thoughts.md
├── memory/continuity.jsonl
├── memory/{facts,findings,hypotheses,decisions,events,pending}.jsonl  # compatibility
├── repo/                         # legacy exact root, or container for registered repo/<repo-id>/ roots
├── corpora/
├── artifacts/
├── reports/
├── scratch/
└── index/
```

`SITUATION.md` and `HANDOFF.md` are generated from safe continuity/workspace state. Explicit sensitive records are excluded.

## Git and image boundary

The entire `workspace/` tree and mutable `.harness` runtime state are ignored by Git. Docker build context excludes them. Awoki source is copied into the image and is not replaced by a broad host repository mount.

## Retrieval boundary

Do not broadly index:

```text
explicit no-RAG records
raw Burp evidence
private keys or environment files
.harness/artifacts/private/
workspace/scratch/
excluded project-policy paths
```

Only allowed content may be sent to the remote embedding or reranking endpoint.


## Backup classification

Authoritative or rebuildable-source data:

```text
workspace/
.harness/state/
.harness/artifacts/
.harness/memory/
.harness/notes.md
.awoki-global/ and separately configured global roots/skills
```

Derived data:

```text
workspace/projects/*/index/
.harness/index/
global awoki_global_fts.sqlite* and index-manifest.json
data/qdrant/
workspace/.lavish/state/
```

Sensitive installation state excluded by default:

```text
.env
.ssh-container/
.opencode-state/
Docker named volumes
```

`make backup-portable` captures the authoritative category and rebuilds lexical indexes on restore. External Git worktree/alternate metadata, special files, and escaping symlinks are rejected rather than silently omitted. `make backup-full` adds derived indexes and stopped Qdrant storage. See `docs/BACKUP_RESTORE.md`.

## OpenCode runtime state

The SSH workflow persists distinct OpenCode directories rather than mounting the host home directory: `.opencode-state/share`, `.opencode-state/local-state`, `.opencode-state/config`, `.opencode-state/cache`, and `.opencode-state/npm`. The host `.opencode-state/` root is secured to `0700`; `.opencode-state/web-auth/` is a separate ignored credential directory (`0700`, single-link password `0600`) mounted read-only only for Web bootstrap; the entrypoint copies the password into container `/run` tmpfs and does not place it in the general runtime snapshot. Neovim data/state and SSH server host keys use named volumes.


## Optional project Burp state

A normal project does not contain `artifacts/burp/`. That subtree is created lazily only when a Burp-specific write/preservation operation is performed. Read-only project lifecycle operations must not create it.

## Repository identity boundary

A project has two supported repository layouts. In legacy mode, `workspace/projects/<project>/repo/` itself is the exact Git worktree top-level. In registered multi-repository mode, `repo/` is only a container and every configured child such as `repo/oathkeeper/` must independently be the exact Git top-level when Git-backed. A nested/mismatched Git root is rejected instead of borrowing another checkout's commit identity. Intentional non-Git registered roots remain supported as `FILESYSTEM_BOUND`. Each registered repository has independent provenance/index/vector/evidence identity and completeness accounting. Repository provenance metadata lives in derived code-index manifests; exact source evidence IDs bind current bytes plus repository identity and are not secret/authorship artifacts.
