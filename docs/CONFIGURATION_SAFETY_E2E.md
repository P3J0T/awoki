# Configuration safety verification — 2026-09-10

## Scope and isolation

Tested the configuration/reliability changes based on commit
`645002b55f7c9424b9be332e9eab7ca0afa91648` on branch
`codex/configuration-safety-e2e`.

A fresh source-only clone used a separate Docker Compose project, image tag,
loopback ports, SSH keypair, generated Web password, Qdrant storage and global
state. The existing installation was not restarted or edited. Burp was disabled
only in the disposable fixture; no real endpoint, key or target data was used.

The install path exercised the shared configuration compiler, `init-awoki.sh`,
the actual Dockerfile build and `run-opencode-ssh`. This was not an automated
walkthrough of every interactive installer prompt.

Runtime: OpenCode/SDK **1.18.30**, Python **3.12**, Qdrant **1.18.2**. The final
test image ID was
`sha256:1c0dd1691c55968e94cbadaa4036f6958cfe170b9841ad3915bef05264a5b75c`.
Source was baked into the image; only test state was mounted writable.

## End-to-end results

A Python HTTP fixture checked the received model ID, path, body shape and exact
authentication header multiplicity/value. Chat requests came from real OpenCode
Web sessions and completed through its streaming provider client. Embedding and
reranker requests came through the actual Awoki MCP server and runtime clients.

| Scenario | Result |
| --- | --- |
| Fresh startup | Real public-key SSH login and authenticated Web health passed |
| Bearer authentication | Exactly one `Authorization: Bearer <synthetic-key>` on chat, embedding and reranker requests |
| Shared-key rotation | `make ai-key-update` reloaded/recreated the service; all three clients used the new key |
| Multiple models | `make ai-model-add` retained the first model; both completed real chat requests |
| Context/output limits | Effective OpenCode config retained 131072/4096 for the first model and 65536/2048 for the second |
| Disabled reranker | Stayed disabled through key rotation; no reranker request was sent |
| Raw Authorization | Lowercase header input compiled correctly; exactly one raw Authorization value arrived on all three paths |
| Custom header | `X-API-Key` arrived once on all three paths, with no extra Authorization header |
| Endpoint and provider changes | Derived reranker URL moved from `/rerank` to `/alt/rerank`; both models survived provider rename |
| Failed reload | One deliberately failed Docker restart returned failure; both files were restored byte-exactly with mode 0600; real requests verified recovery with the previous key |
| Final image recreation | SSH, Web, both chat models, embedding and reranking passed again after recreation |
| Persistent retrieval | Synthetic memory was captured, embedded, written to Qdrant and retrieved with a current vector index; reranking did not fall back |
| MCP self-checks | The actual bounded 15-test, 3-test and 3-test checks all passed through MCP |

The served embedding ID was `jina-code-embeddings`; reranking used `bge-reranker`;
chat used `qwen3.8-27b` plus `synthetic-second-model`. The fixture returned synthetic
64-dimensional vectors. That dimension is a test choice, not a claim about the
real embedding deployment's dimensions.

## Regression checks

- macOS `make validate`: 607 tests, 7 environment-dependent skips, passed; actual SDK type/session-hook checks, source checks, shell syntax and code-search fixtures passed.
- Linux source validation and full suite in the built dependency environment: 607 tests, 1 environment-dependent skip, passed, with external networking disabled.
- All 12 offline provider-contract cases passed. This smaller reusable check is available with `make ai-config-test`; its chat leg alone is config-derived HTTP, unlike the real OpenCode E2E run above.
- The Docker build passed dependency constraints, `pip check`, mandatory parser checks, code-search fixtures and tmux smoke testing.
- `git diff --check` passed.

Testing exposed an MCP launcher issue with explicit non-default snapshot layouts;
the launcher now honors that override while keeping default appliance launches at
`/awoki`. A nested regression test also exceeded its fixed 20-second deadline on
macOS. Its dispatch/bounds checks now use deterministic subprocess outcomes,
including explicit failure/timeout coverage. The production deadline and all
underlying tests are unchanged; actual MCP self-checks were separately exercised
successfully in Linux. Existing subprocess resource warnings remain non-fatal.

## Limits

This verifies request structure, configuration transitions and runtime integration.
It does **not** validate a real API key, endpoint availability, model quality,
production embedding dimensions, vector migration, full interactive installer UX,
or every Awoki feature. No remote CI was triggered and no tag was created/moved.
Paired-file rollback handles raised write/reload errors; power-loss/process-kill
atomicity is not claimed.

The disposable fixture scripts and sanitized phase reports were retained locally
under `/private/tmp/awoki-e2e.j2R0my` for inspection. They are temporary run artifacts,
not required installation files or a portable automated E2E runner.
