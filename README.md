# Awoki

Awoki is a Docker-first companion to OpenCode for long-running software and security investigations. It keeps project memory, repository retrieval and evidence tied to their source instead of treating chat history as truth.

OpenCode and Awoki MCP run together; Qdrant stores rebuildable semantic vectors. Structural/FTS review works without remote embeddings. Reranking and Burp integration are optional.

Actively stabilizing: the priority is useful real-world review workflows and less complexity, not more machinery. Release identity lives in [`pyproject.toml`](pyproject.toml) and [`.harness/manifest.json`](.harness/manifest.json); history lives in [`CHANGELOG.md`](CHANGELOG.md).

The [detailed project guide](docs/PROJECT_GUIDE.md) preserves the full design rationale, architecture diagrams and extended workflow examples.

## First install

On the host, install Docker with Compose v2, Git, OpenSSH, Make and Python 3.12+. A host OpenCode installation is not required.

```bash
git clone https://github.com/P3J0T/awoki.git
cd awoki
./install-awoki.sh
```

The wizard preserves existing settings, configures SSH/Web and optional custom AI services, then shows a configuration review before you explicitly choose **BUILD/START Docker**.

OpenCode Web defaults to **http://127.0.0.1:4096**. Show its generated password with:

```bash
make opencode-web-password
```

For the terminal UI, use the SSH command printed by the installer, then:

```bash
cd /awoki
tmux new -A -s awoki
awoki-opencode
```

Web and SSH attach to the same backend. tmux survives SSH disconnects, not container recreation. For a stopped installation, run `make opencode-ssh-up` from the host checkout.

If startup fails, fix that failure first; do not run `make opencode-runtime-check` against an old or partially started container. Do not use `docker compose down -v`: it removes named volumes. For stale-container/reclone problems, use the guided installer and [runtime troubleshooting](docs/OPENCODE_SSH.md).

See [INSTALL.txt](INSTALL.txt) for the complete installer, ZIP, VS Code and troubleshooting paths.

## Upgrade with a backup

Run on the **host in your existing checkout**, not inside `/awoki`. Finish active work and stop host-local OpenCode/MCP processes first. The backup command below stops this installation's Compose services and leaves them stopped until recreation.

```bash
(
  set -eu
  test "$(git branch --show-current)" = main
  test -z "$(git status --porcelain)" || {
    echo "Stop: review local changes before upgrading."
    exit 1
  }

  awoki_upgrade_backup="$(mktemp -d ../awoki-upgrade.XXXXXX)"
  git bundle create "$awoki_upgrade_backup/source.bundle" --all
  make backup-full \
    BACKUP_DIR="$awoki_upgrade_backup" \
    BACKUP_STOP_CONTAINERS=1

  git pull --ff-only origin main
  make opencode-recreate \
    OPENCODE_RECREATE_ARGS="--validate --opencode-latest"
)
```

The archive is verified before backup creation reports success; keep its `.sha256` sidecar. The Git bundle preserves source history separately. If any step fails, stop there—do not reset your checkout or delete data to force the update.

**Backup scope:** full mode includes project/global data, indexes and local Qdrant storage, but excludes `.env`, SSH client keys and OpenCode state by default. To explicitly include those sensitive files, add both `BACKUP_INCLUDE_SECRETS=1` and `BACKUP_INCLUDE_OPENCODE_STATE=1` to the backup command. Such archives contain credentials/conversations: store securely and do not upload them. Docker named volumes are not archived.

Upgrading in place preserves the existing `.env` and ignored user config; do not replace them with examples or rerun first-install copying steps. Recreate validates, rebuilds, starts and checks the runtime while preserving Qdrant data. Reconnect afterward; running tmux sessions end during the upgrade.

OpenCode normally resolves latest at build time, with a matching plugin/SDK; it never silently auto-updates inside a running container. To retain an operator-selected tested version, replace `--opencode-latest` with `--opencode-safe <VERSION>`. Add `--no-cache` when a full uncached rebuild is wanted. See [dependency policy](docs/DEPENDENCY_UPDATES.md).

For rollback, verify the archive and restore into a separate compatible checkout first. Raw Qdrant restores require matching image/embedding identity, dimensions and collections. Follow [backup and restore](docs/BACKUP_RESTORE.md); do not force a restore over live data.

## Custom chat, embeddings and reranking

Use one guided configuration flow, during installation or afterward:

```bash
make ai-configure
```

Prompts show current/default values and formats. Fresh model defaults are:

| Setting | Default/example |
| --- | --- |
| Provider base URL | Supply your own, e.g. `https://your-provider.example/v1` |
| Chat model | `qwen3.8-27b` |
| Embedding model | `jina-code-embeddings` |
| Embedding vector size | `768` — confirm against your deployment |
| Reranker model | `bge-reranker` |
| Reranker URL | Derived from the base URL, e.g. `https://your-provider.example/rerank` |
| Chat context / output | `131072` / `8192` tokens — confirm your model's limits |
| Authentication | `bearer`: `Authorization: Bearer <raw key>` |

Choose `header` for a raw-key header such as `X-API-Key` or raw `Authorization`. Header names are case-insensitive. Paste only the raw key; an accidental leading `Bearer ` is removed. The same selected authentication form is used for all three services.

The compiler stores the shared key once in `.env`, generates `.opencode-state/config/opencode.jsonc` and reloads the running service. Both files still exist—you normally do not need to edit them by hand. The tracked root `opencode.jsonc` contains Awoki's project settings, not personal credentials.

```bash
make ai-key-update     # hidden prompt; rotate only the shared key, then reload
make ai-model-add      # add/update another chat model and its context/output limits
make ai-config-compile AI_CONFIG_ARGS="--dry-run"  # redacted preview only
```

Key rotation preserves model settings and disabled reranking. Additional models retain existing models/provider options. Reported write/reload errors restore previous files and attempt runtime recovery; power-loss atomicity is not claimed. Changing embedding identity/dimensions does not migrate existing vectors.

For standard OpenCode provider login use `make opencode-auth`. Advanced manual settings belong in `.opencode-state/config/opencode.jsonc`; validate/reload with `make opencode-config-reload`. See [provider setup](docs/OPENCODE_SSH.md) and [INSTALL.txt](INSTALL.txt).

## Start a review

Create a project and clone its target repository from the host checkout:

```bash
python3 .harness/project.py create review1
git clone <repository-url> workspace/projects/review1/repo/target
python3 .harness/project.py repo-add --default review1 target repo/target
```

Then ask OpenCode:

> Resume project review1. Prepare target for local review. Review its authentication flow; distinguish verified behavior from hypotheses and gaps. Verify your findings before answering.

`repository_prepare_start` manages detached preparation: `LOCAL_READY` means structural/FTS state is current; `FULL_READY` additionally requires current vector membership and configured backend readiness. Request full semantic preparation explicitly—it sends eligible material to your configured embedding service. Opening a project does not authorize that upload.

Use natural follow-ups: “Show the evidence,” “What would disprove that?” or “Continue where we left off.” OpenCode's TODOs provide a bounded active working set across compaction; durable project knowledge remains separate from conversational history.

## Boundaries

- Search discovers candidates; exact current source supports behavioral claims. `code_exact_search` complements conceptual retrieval.
- Evidence retains source identity, scope and corrections. Model output is not automatically true.
- Private chain-of-thought is not persisted. Explicit secret/no-RAG records are excluded from embeddings/reranking.
- Hostile repository code needs a separate credential-free sandbox; the shared OpenCode runtime is not a same-user security boundary.
- Burp is optional; network sends and other side effects require explicit authorization.

## Checks and guides

```bash
make validate                # source, regression, shell and plugin checks
make opencode-runtime-check  # only after successful startup
make runtime-config          # supported runtime configuration diagnostic
make ai-config-test          # local synthetic provider contracts; Python deps required
```

The small provider-contract probe checks request structure, not real-key validity or full OpenCode behavior. See the separately recorded [real E2E results](docs/CONFIGURATION_SAFETY_E2E.md).

- [Install/update/troubleshooting](INSTALL.txt) · [SSH/Web/tmux](docs/OPENCODE_SSH.md) · [Backup/restore](docs/BACKUP_RESTORE.md)
- [Commands](docs/COMMANDS.md) · [Code search](docs/CODE_SEARCH.md) · [Continuity](docs/CONTINUITY.md) · [Burp](docs/BURP.md)
- [Architecture](docs/ARCHITECTURE.md) · [Reliability](docs/RELIABILITY.md) · [Operator reference](docs/OPERATOR_REFERENCE.md)
- [Project identity](docs/AWOKI_IDENTITY.md) · [Usefulness evaluation](docs/USEFULNESS_EVALUATION.md) · [Changelog](CHANGELOG.md) · [License](LICENSE)
