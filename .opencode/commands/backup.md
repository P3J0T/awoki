---
description: Create and verify an Awoki runtime backup without silently including credentials
---

Create an Awoki runtime backup only after confirming the requested mode and sensitivity.

1. Prefer `make backup-portable` for migration to another installation.
2. Use `make backup-full` only when the user explicitly wants local SQLite/Qdrant state preserved and every Awoki Compose service is stopped.
3. Never add `BACKUP_INCLUDE_SECRETS=1` or `BACKUP_INCLUDE_OPENCODE_STATE=1` unless the user explicitly requests those sensitive paths.
4. Keep the output outside the Awoki repository and archived data roots.
5. Verify the generated archive with `make backup-verify BACKUP=...`.
6. Report the archive path, `.sha256` sidecar, mode, secret/OpenCode-state inclusion, and whether services were stopped.
7. Do not claim portability of a full raw-Qdrant backup across different Qdrant versions, embedding dimensions, collection settings, normalisation, or actual served model/revision. Require `AWOKI_EMBEDDING_DEPLOYMENT_ID` for a non-empty full Qdrant restore unless the user explicitly accepts force.

Read `docs/BACKUP_RESTORE.md` before destructive restore guidance.
