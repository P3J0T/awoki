# Awoki backup and restore

Awoki runtime data is deliberately split into authoritative data and rebuildable indexes. Use the built-in backup tool rather than copying individual database files by hand.

## Commands

From the Awoki repository root:

```bash
make backup-portable
make backup-full
make backup-inspect BACKUP=/path/to/awoki-*.tar.gz
make backup-verify BACKUP=/path/to/awoki-*.tar.gz
make restore BACKUP=/path/to/awoki-*.tar.gz
```

Backups are written to `../awoki-backups` by default. Override this with:

```bash
make backup-portable BACKUP_DIR=/secure/external/path
```

The destination must be outside every archived Awoki data root. Each archive and its `.sha256` sidecar are created with mode `0600`. Creation re-opens and verifies the finished archive before reporting success; a failed self-check removes both files.

## Portable backup

A portable backup contains the authoritative/rebuildable-source data needed to move Awoki to a clean installation:

- `workspace/`, including project repositories, canonical continuity, notes, reports, corpora, artifacts, and scratch data;
- `.harness/state`, excluding the installation-specific layout marker and backup lock;
- `.harness/artifacts`, `.harness/memory`, and `.harness/notes.md`;
- repository-local `.awoki-global/` data;
- a separately configured `AWOKI_GLOBAL_ROOT` when it is outside `.awoki-global/`;
- a separately configured global skills directory when it is not already inside a captured global root.

Before capture, repository Git metadata is checked for portability. Linked worktrees, submodules, or alternates whose `.git` metadata/object store resolves outside `workspace/` are rejected rather than silently producing an incomplete repository. Convert them to self-contained clones first. Sockets, devices, and FIFOs inside captured data are also rejected instead of omitted.

It excludes derived data:

- project `index/` directories, including `awoki_code.sqlite` and code-index manifests;
- `.harness/index/`;
- global SQLite FTS databases and index manifests;
- `data/qdrant/`;
- Lavish runtime state.

A portable restore invalidates any existing derived indexes and rebuilds lexical indexes for every restored project and every restored global root by default. It does not automatically index repository source code, preserving `/codebase`'s explicit code-indexing policy. Rebuild semantic vectors after Qdrant and the remote embedding endpoint are available:

```bash
make index-vector
```

Or request vector rebuilding during restore:

```bash
make restore \
  BACKUP=/path/to/awoki-portable-*.tar.gz \
  RESTORE_REINDEX=vector
```

## Full backup

A full backup adds the local derived search state:

- project indexes under `workspace/projects/<project_id>/index/`, including structural code SQLite state;
- `.harness/index/`;
- global FTS/index state;
- raw `data/qdrant/` storage.

Every Awoki Compose service must be stopped. Full mode captures mutable SQLite/project indexes as well as raw Qdrant storage, so `BACKUP_ALLOW_LIVE=1` is intentionally rejected for full backups. Use `BACKUP_STOP_CONTAINERS=1` or stop both Compose projects first.

Full restore checks these compatibility-sensitive values before applying Qdrant/index state:

```text
AWOKI_VECTOR_SIZE
AWOKI_QDRANT_COLLECTION
AWOKI_CODE_QDRANT_COLLECTION
AWOKI_EMBEDDING_PROVIDER
AWOKI_EMBEDDING_MODEL
AWOKI_EMBEDDING_NORMALIZE
AWOKI_EMBEDDING_DEPLOYMENT_ID
Qdrant image reference and matching digest/ID when available
```

A mismatch blocks restore unless `RESTORE_FORCE=1` is explicitly supplied. The
structural code SQLite database is derived and schema/version checked when opened;
an incompatible database is discarded and rebuilt rather than migrated in place.
The code Qdrant collection is also derived, but a raw full restore must use the
same configured code collection and embedding identity or be followed by a clean
code reindex.  `AWOKI_EMBEDDING_MODEL=text-embeddings-inference` is only the TEI request label, so set `AWOKI_EMBEDDING_DEPLOYMENT_ID` to the actual served model and preferably its immutable revision. Non-empty Qdrant state is blocked when that identity is missing on either installation. A mutable Qdrant tag such as `qdrant/qdrant:latest` is blocked when matching image identity cannot be proven; pull/inspect the same image first, pin an immutable release, or use portable restore. A portable restore followed by reindexing is safer when the Qdrant version, collection, embedding model, normalisation, or vector dimension changed.

The current Compose default is the release pin `qdrant/qdrant:v1.18.2`, recorded in `.harness/runtime-dependencies.lock.json`; an explicit `AWOKI_QDRANT_IMAGE` override may still select another image and is therefore captured/checked during full restore. Raw full backups remain less portable than portable backups because raw Qdrant storage is version-sensitive even with a pinned default. Full mode backs up only the repository's bind-mounted `data/qdrant/`; it does not snapshot an independently hosted or remote Qdrant server. Use that server's snapshot/backup mechanism separately.

## Quiescence

By default, backup and restore refuse to run while Awoki Compose services are active. If the Docker CLI exists but Compose state cannot be queried, the command fails rather than assuming services are stopped. Host-local OpenCode/MCP processes and host-side edits are not Docker services and cannot be reliably discovered, so stop those and pause writes manually before a consistent capture or any restore. Stop Compose services yourself:

```bash
make docker-down
```

Or explicitly let the helper stop both Compose projects:

```bash
make backup-portable BACKUP_STOP_CONTAINERS=1
make backup-full BACKUP_STOP_CONTAINERS=1
make restore BACKUP=/path/to/archive.tar.gz RESTORE_STOP_CONTAINERS=1
```

Containers stopped by the helper remain stopped. Restart them deliberately after verification. Standard MCP/OpenCode launchers and runtime Make targets honour `.harness/state/backup-restore.lock`, refuse to start during an active operation, and remove a stale regular-file lock whose owning PID is dead. Direct `docker compose` commands can bypass that guard, so do not run them concurrently. A non-regular/symlink lock is rejected.

`BACKUP_ALLOW_LIVE=1` is available only as an explicit acknowledgement for a potentially inconsistent **portable** capture. It is rejected for full backups. Restore never runs against any live Awoki Compose service; there is no live-restore override.

## Secrets and OpenCode state

By default, both backup modes exclude:

- `.env`;
- `.ssh-container/` and its client private key;
- `.opencode-state/`;
- OpenCode/npm caches;
- Docker named volumes used for Neovim state and SSH server host keys.

Explicit options:

```bash
make backup-full BACKUP_INCLUDE_OPENCODE_STATE=1
make backup-full BACKUP_INCLUDE_SECRETS=1
make backup-full \
  BACKUP_INCLUDE_OPENCODE_STATE=1 \
  BACKUP_INCLUDE_SECRETS=1
```

`BACKUP_INCLUDE_OPENCODE_STATE=1` captures `.opencode-state/share`, `local-state`, and `config`, but still excludes `cache` and `npm`. OpenCode state may contain provider credentials, sessions, and conversation data, so the archive is marked sensitive.

`BACKUP_INCLUDE_SECRETS=1` captures `.env` and `.ssh-container/`. Transfer such archives only through an encrypted channel and store them as secrets.

Even a default backup can contain sensitive project repositories, continuity, evidence, or explicitly saved secret/no-RAG records. “Secrets excluded” only means known installation credential paths were omitted; it does not make project data public-safe.

## Compose storage coverage

Every host bind mount in both Compose files is classified and validated by `.harness/validate.py`:

| Host runtime path | Portable | Full | Notes |
|---|---:|---:|---|
| `workspace/` | yes | yes | Project repositories, continuity, notes, reports, corpora, artifacts; project `index/` is full-only |
| `.harness/state/` | yes | yes | Excludes generated layout marker, maintenance lock, and tracked root README |
| `.harness/artifacts/`, `.harness/memory/`, `.harness/notes.md` | yes | yes | Known tracked artifact READMEs are excluded; user-created nested READMEs remain data |
| `.harness/index/` | no | yes | Derived local FTS/index state |
| `.awoki-global/` and configured global roots/skills | yes | yes | Portable excludes only exact generated global FTS/index files |
| `data/qdrant/` | no | yes | Raw local bind-mounted Qdrant storage; requires quiescence and compatibility checks |
| `.opencode-state/share`, `local-state`, `config` | opt-in | opt-in | `BACKUP_INCLUDE_OPENCODE_STATE=1`; may contain credentials/conversations |
| `.opencode-state/cache`, `.opencode-state/npm` | no | no | Rebuildable caches |
| `.ssh-container/` | opt-in | opt-in | `BACKUP_INCLUDE_SECRETS=1` |
| Neovim and SSH-host-key named volumes | no | no | Recreated on the destination |
| tmpfs and `workspace/.lavish/state` | no | no | Ephemeral runtime state |

Adding a new host bind mount without updating this classification makes repository validation fail.

Docker named volumes are intentionally not captured. Neovim state can be re-created, and the SSH server host-key volume is regenerated. When the server host key changes, remove the stale entry from `.ssh-container/known_hosts` or recreate local SSH state.

Archive entries preserve regular permission bits and safe relative symlinks, but ownership is normalised and restored as the user running the restore. ACLs, extended attributes, macOS resource forks, and filesystem-specific sparse/compression metadata are not preserved. Repair Linux ownership deliberately when the destination container UID/GID differs.

## Restore into a new installation

1. Install the same or a compatible Awoki source version.
2. Copy the archive and its `.sha256` sidecar to the new machine.
3. Configure the destination `.env`, especially the embedding endpoint, vector size, collection, global root, and skills directory.
4. Keep both Compose projects stopped.
5. Inspect and verify:

```bash
make backup-inspect BACKUP=/path/to/awoki-portable-*.tar.gz
make backup-verify BACKUP=/path/to/awoki-portable-*.tar.gz
```

6. Restore:

```bash
make restore BACKUP=/path/to/awoki-portable-*.tar.gz
```

The restore tool first checks free space for private staging and for any restored global/skills targets on separate filesystems.

The restore tool:

- requires the `.sha256` sidecar;
- treats SHA-256 as an integrity check, not authentication—the archive and sidecar must arrive through a trusted channel;
- validates the archive format and member paths;
- rejects absolute paths, traversal, special files, escaping links, duplicate/undeclared members, manifest count/byte mismatches, wrong file/directory payload roots, and members excluded by the declared portable/full mode;
- refuses to overwrite existing runtime data by default, including canonical repository `README.md` files rather than ignoring them by basename;
- restores configured global roots to the destination machine's resolved paths rather than reproducing source-machine absolute paths, rejects filesystem-root/home/other overly broad targets, and rejects overlapping targets after resolving symlinked parents;
- re-runs `init-awoki.sh` to regenerate the layout marker for the new installation;
- rebuilds lexical indexes automatically for a portable backup; multiple global roots are rebuilt lexically, while vector-mode restore limits global Qdrant rebuilding to the preferred repo-local Docker/SSH root and reports the alternate-root follow-up;
- leaves code indexing opt-in;
- reports a reindex warning without discarding the successfully restored canonical data.

When restoring into a non-empty installation, inspect the destination first. Explicit replacement is:

```bash
make restore \
  BACKUP=/path/to/archive.tar.gz \
  RESTORE_FORCE=1
```

`RESTORE_FORCE=1` clears only managed Awoki runtime targets represented by the archive and, for a portable restore, stale local/Qdrant indexes. The archive is fully extracted to private staging before any destination data is cleared, and tracked harness README files are preserved. Applying multiple runtime roots is not a cross-filesystem transaction: an operating-system or hardware failure during the final apply can still leave a partial restore, so retain the source archive and do not delete the old installation until post-restore checks pass.

## Direct CLI

The Make targets wrap:

```bash
.harness/bin/awoki-backup create --mode portable --output-dir ../awoki-backups
.harness/bin/awoki-backup create --mode full --output-dir ../awoki-backups
.harness/bin/awoki-backup inspect /path/to/archive.tar.gz
.harness/bin/awoki-backup verify /path/to/archive.tar.gz
.harness/bin/awoki-backup restore /path/to/archive.tar.gz --reindex auto
```

Use `--help` for the explicit `--force`, `--stop-containers`, `--include-opencode-state`, and `--include-secrets` switches. `--allow-live` exists only on `create` and only applies to portable capture.

## Post-restore verification

```bash
.harness/bin/awoki doctor
make doctor
make validate
```

Then start the preferred deployment:

```bash
make install-opencode-ssh
```

Open a restored project and verify its generated views and retrieval status before relying on prior conclusions.
