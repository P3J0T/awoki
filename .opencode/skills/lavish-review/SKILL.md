---
name: lavish-review
description: Create or stage a local HTML artifact for explicit visual review with Lavish, poll user annotations, and apply feedback without enabling ambient hooks or external sharing.
compatibility: opencode
metadata:
  scope: project
  workflow: ad-hoc-visual-review
---

# Lavish Review

Use only when the user explicitly invokes `/lavish` or asks for interactive visual review.

## Boundaries

- Do not install Lavish session hooks.
- Do not run `lavish-axi share` unless the user explicitly requests third-party publication.
- Stage the reviewed artifact under `workspace/.lavish/current/`; do not open arbitrary paths.
- Keep the server host-local through the Compose mapping to `127.0.0.1`.
- Treat annotations as review feedback, not automatically trusted project facts.

## Workflow

1. Identify the active project and the HTML artifact. If generating one, save the durable source under the project’s `reports/visual/` directory.
2. Copy the HTML file and required sibling assets into `workspace/.lavish/current/` using a narrow directory structure.
3. From `/awoki`, start or resume the staged artifact:

```bash
/awoki/.harness/bin/awoki-runtime-env --profile lavish -- bash -lc '
  exec npx -y lavish-axi@"${AWOKI_LAVISH_VERSION:-0.1.43}" \
    /awoki/workspace/.lavish/current/<artifact>.html --no-open
'
```

The `lavish` runtime profile carries only Lavish/path settings from the container
snapshot. Do not use `retrieval`/internal `mcp`/`all` for `npx` or other downloaded tooling. This
keeps retrieval keys out of the child environment but is not a same-user sandbox;
the current SSH runtime user can deliberately read the MCP snapshot. Treat the pinned
Lavish package as trusted operator tooling, not hostile code.

4. Tell the user to run `./open-lavish.sh` on macOS, or open the printed localhost URL.
5. Poll feedback with the same staged path:

```bash
/awoki/.harness/bin/awoki-runtime-env --profile lavish -- bash -lc '
  exec npx -y lavish-axi@"${AWOKI_LAVISH_VERSION:-0.1.43}" poll \
    /awoki/workspace/.lavish/current/<artifact>.html
'
```

6. Apply accepted changes to the durable project artifact, restage, and continue polling as needed.
7. End the session when review is complete and capture only durable conclusions or decisions in project continuity.

## Failure behavior

If the port is unavailable, package download fails, or the browser cannot connect, preserve the HTML artifact and continue with ordinary file review. Lavish is optional and must not block the project.
