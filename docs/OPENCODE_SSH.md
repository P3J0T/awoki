# OpenCode over SSH container

This is the preferred workflow when OpenCode must not run on the host.

## Architecture

```text
macOS host
  ├─ browser -> OpenCode Web on 127.0.0.1:4096
  ├─ Burp MCP on 127.0.0.1:9876
  └─ SSH client to 127.0.0.1:2222

Docker networks
  ├─ awoki-data (internal): qdrant at http://qdrant:6333
  └─ awoki-egress: awoki-opencode-ssh remote endpoint and host access
       one OpenCode Web/server backend
       SSH TUI via `awoki-opencode` -> `opencode attach`
       Awoki MCP local child process
       Neovim and tmux
       immutable /awoki source
       explicit writable runtime mounts
```

OpenCode runs inside `awoki-opencode-ssh`. With the stock `AWOKI_OPENCODE_WEB_ENABLED=1`, the entrypoint starts one authenticated `opencode web` backend and supervises it alongside `sshd`; `awoki-opencode` attaches the SSH TUI to that same backend so browser/TUI share OpenCode session state. Awoki MCP runs locally inside the same container through `.harness/bin/mcp-auto`; Docker-in-Docker and the Docker socket are not used.

## Install and connect

```bash
cp .env.example .env
./init-awoki.sh
make install-opencode-ssh
make opencode-ssh-client-check  # host keypair + container authorized key + real login
make opencode-web-password   # explicit secret display; username defaults to opencode
# Browser: http://127.0.0.1:${AWOKI_OPENCODE_WEB_PORT:-4096}
ssh -i "$PWD/.ssh-container/id_ed25519" -o IdentitiesOnly=yes -o UserKnownHostsFile="$PWD/.ssh-container/known_hosts" -o StrictHostKeyChecking=accept-new -p ${AWOKI_OPENCODE_SSH_PORT:-2222} op@127.0.0.1
cd /awoki
awoki-opencode
```

Startup treats SSH client readiness as a hard contract, not a warning. Before `make install-opencode-ssh` / `make opencode-ssh-up` succeeds, Awoki requires the host private/public key pair to exist and match, requires the running container's `/home/op/.ssh/authorized_keys` to match that public key, and performs a real `BatchMode` public-key login. `make opencode-ssh-client-check` repeats that exact gate and is also run by the interactive installer immediately before it can print installation success.

`awoki-opencode` is the supported interactive wrapper. With Web enabled it loads the runtime-only password from `/run/awoki/opencode-web-password` and executes `opencode attach` without putting the password in process arguments. With `AWOKI_OPENCODE_WEB_ENABLED=0` it falls back to standalone `opencode`. Project configuration remains `opencode.jsonc`; project skills and commands are under `.opencode/`.

A replaced checkout at the same macOS pathname is handled explicitly. The ignored `layout_initialized.json` carries a per-checkout `runtime_instance_id`, and both long-lived OpenCode Compose services are labeled with it. Before Qdrant bind probing, the launcher inspects existing service containers. It also classifies the exact Docker owner of the configured SSH/Web ports, because Docker Desktop can leave an orphan publishing a port even when the new `docker compose ps` no longer enumerates that container. Same-checkout stale owners are identified by Compose project/service plus checkout/runtime identity; `/host_mnt/...` is treated as the equivalent macOS path only when it resolves to the current checkout. Safe cleanup removes only the exact stale container IDs with `docker rm -f` and never requests volume deletion, so named volumes and host Qdrant data remain intact. A different-checkout/service owner or a non-Docker listener is never removed automatically; startup fails with the owner details instead.

Fresh image builds resolve OpenCode in **latest / untested** mode by default. The CLI is
resolved first, then `@opencode-ai/plugin` and `@opencode-ai/sdk` are materialized at the
same version and verified by the runtime compatibility gate. The running container does
not auto-update. To build an operator-selected last-known-good stack instead:

```bash
make install-opencode-ssh OPENCODE_INSTALL_MODE=safe OPENCODE_SAFE_VERSION=<exact-version>
# Later recreation:
make opencode-recreate OPENCODE_RECREATE_ARGS="--validate --no-cache --opencode-safe <exact-version>"
```

Return to the moving latest channel with
`make opencode-recreate OPENCODE_RECREATE_ARGS="--validate --no-cache --opencode-latest"`.
`make opencode-runtime-check` verifies the resolved CLI/plugin/SDK tuple against the
image record in `/usr/local/share/awoki/opencode-runtime.json`.

## Network paths

Inside the container:

```text
Qdrant: http://qdrant:6333
Burp MCP on macOS: http://host.docker.internal:9876
Remote embeddings: operator-configured OpenAI-compatible endpoint
Remote reranker: optional operator-configured endpoint
```

Qdrant host ports, SSH, OpenCode Web, and Lavish are published only on host loopback. OpenCode Web binds `0.0.0.0` only inside the container so Docker can forward the loopback host port; mDNS is not enabled. Non-loopback Web exposure is intentionally out of scope until an authenticated TLS reverse-proxy design is added. No host network mode is required.

## Writable mounts

Awoki source is baked into the image. The normal service mounts only:

```text
./workspace                    -> /awoki/workspace
./.harness/state               -> /awoki/.harness/state
./.harness/index               -> /awoki/.harness/index
./.harness/artifacts           -> /awoki/.harness/artifacts
./.harness/memory              -> /awoki/.harness/memory
./.harness/notes.md            -> /awoki/.harness/notes.md
./.awoki-global                -> /global
.opencode-state/share           -> OpenCode application/session data
.opencode-state/local-state     -> OpenCode local state
.opencode-state/config          -> OpenCode user configuration; personal provider/model config lives in `.opencode-state/config/opencode.jsonc`
.opencode-state/cache           -> OpenCode/plugin and Neovim caches
.opencode-state/npm             -> npm/npx cache
.opencode-state/web-auth          -> read-only `/awoki-web-auth`; generated Web password at rest
host-generated SSH public key    -> injected through `AWOKI_SSH_AUTHORIZED_KEY` and installed as `/home/op/.ssh/authorized_keys` at container start
named volume                   -> persistent SSH server host keys
named volumes                  -> Neovim data/state
```



### Custom provider configuration

Use the host file `.opencode-state/config/opencode.jsonc` for personal provider/model settings. It is bind-mounted as `/home/op/.config/opencode/opencode.jsonc`, ignored by Git, and excluded from the Docker build context. Do not put personal provider secrets into Awoki's tracked root `opencode.jsonc` unless you intentionally want a source-level configuration change.

After editing the user file on an installed runtime, run `make opencode-user-config-check` and `make opencode-config-reload`. A config reload restarts the OpenCode SSH/Web service (so live TUI/Web/tmux processes end) but does not rebuild the image or delete persisted state. For provider credentials supported by OpenCode's auth flow, use `make opencode-auth`.

### OpenCode Web authentication and file permissions

The stock Web path does not use a fixed `awoki` password. `run-opencode-ssh` calls `.harness/bin/prepare-opencode-web-auth`, which secures the host `.opencode-state/` root as `0700`, creates `.opencode-state/web-auth/` as `0700`, and keeps its password file as a single-link regular file with mode `0600`. The password is generated with Python `secrets.token_urlsafe(32)` unless the operator explicitly sets `AWOKI_OPENCODE_WEB_PASSWORD`. Existing generated passwords are preserved across normal starts. Symlinked/non-regular auth paths and hard-linked password files are rejected.

Compose mounts the auth **directory** read-only at `/awoki-web-auth` rather than binding the password as a single host file. At container start, root validates that source and copies only the password into `/run/awoki/opencode-web-password` on tmpfs as `op:op 0600`. The plaintext is then supplied to OpenCode through `OPENCODE_SERVER_PASSWORD` only in the Web/attach process environment because OpenCode's built-in HTTP Basic Auth requires the original password and does not accept a hash. It is deliberately absent from `docker compose config`, the service environment, the runtime snapshot, and process arguments; the Web child reads the tmpfs file itself before exporting the secret into its own environment. Same-`op` processes are not considered isolated from one another; this improves accidental exposure, not hostile same-user isolation.

Retrieve the current password deliberately with:

```bash
make opencode-web-password
```

An explicit `AWOKI_OPENCODE_WEB_PASSWORD=...` override is supported, including a weak value such as `awoki` for disposable loopback-only testing, but it is not the default. For stronger at-rest storage, the backlog tracks a future macOS Keychain/secret-broker integration that can materialize the secret only at runtime.


### macOS / Docker Desktop SSH-key bootstrap

Awoki does **not** bind-mount `.ssh-container/authorized_keys` as an individual
host file. Docker Desktop for macOS can reject a single-file bind through its
`/host_mnt` bridge even when a bind of the containing `/Users/...` directory
works correctly.

The supported flow is:

```text
macOS host
  .ssh-container/id_ed25519      private; host-only
  .ssh-container/id_ed25519.pub  public
            |
            v
run-opencode-ssh validates the public key
            |
            v
AWOKI_SSH_AUTHORIZED_KEY (public key only)
            |
            v
container validates + installs /home/op/.ssh/authorized_keys (0600, op:op)
```

The public key may be visible in Docker container configuration; it is not a
secret. The private key is never mounted or passed through Compose. Starting the
SSH container without the Awoki launcher leaves the bootstrap value empty and
the entrypoint fails closed with a clear error.

The private SSH client key remains on macOS. The `op` account has no usable password, password authentication is disabled, and `sudo` is not installed or granted. For deliberate container administration, run a root shell from the host:

```bash
docker compose -f docker-compose.opencode.yml exec -u root awoki-opencode-ssh bash
```

The immutable `/awoki` tree is a runtime appliance, not an Awoki development
checkout. It may intentionally have no top-level `.git`, and source files may be
read-only to `op`. An agent asked to modify Awoki must first run `make dev-preflight` (or `.harness/bin/awoki-dev-preflight`) and require it to pass; the preflight verifies a writable
top-level Git checkout. If `/awoki` fails that check, it must stop rather than
trying `sudo`, `su`, `chown`, `chmod`, `/root` discovery, or ownership changes.
Develop Awoki in the host checkout or a separate writable dev container, then
rebuild/recreate this runtime image.

## Recommended daily workflow: tmux + OpenCode

Node 22 is pinned in the image for OpenCode and ad-hoc Lavish. Both Neovim and tmux are installed. tmux is the recommended wrapper for interactive Awoki/OpenCode work because it keeps the terminal process alive when the SSH transport disappears. It is not part of Awoki's epistemic correctness model and does not replace Awoki's durable project/continuity state.

For macOS editor use, the preferred graphical workflow is VS Code **Remote - SSH** connecting to the same Awoki SSH endpoint. No OpenCode VS Code extension is required: open `/awoki` or the exact container-side managed repository in VS Code and run the commands below in VS Code's integrated remote terminal. This avoids host/container path translation and does not create another OpenCode backend.

After SSH login:

```bash
cd /awoki
tmux new -A -s awoki
awoki-opencode
```

`tmux new -A -s awoki` is intentionally idempotent for normal use: it creates the terminal session on the first login and reattaches on later logins. The OpenCode Web backend is a separate entrypoint-managed process, so it remains available to the browser even if SSH/tmux disappears. If SSH drops, reconnect and run the same tmux plus `awoki-opencode` commands; do not recreate the container just to recover the terminal.

Detach deliberately with `<prefix> d`. Both `Ctrl-b` and `Ctrl-a` are valid prefixes, so `Ctrl-b d` and `Ctrl-a d` both work. The OpenCode process continues inside tmux after detach.

A practical layout is:

```text
awoki:1  OpenCode
awoki:2  shell / repository navigation
awoki:3  tests / long-running jobs
awoki:4  logs
```

Create extra windows with `<prefix> c` and switch with normal tmux bindings. The default Neovim configuration is intentionally minimal and loads no plugin manager or third-party code. tmux uses a vendored gpakosz/Oh my tmux! snapshot: the upstream main file is root-owned under `/opt/oh-my-tmux`, `/home/op/.tmux.conf` links to it, and `/home/op/.tmux.conf.local` is the writable Awoki customization layer. No remote installer runs during container startup.

**Persistence boundary:** tmux sessions survive SSH disconnects but do not survive container stop/recreation. The Web backend also ends on container recreation and is restarted by the entrypoint; OpenCode application/session data remains on the mounted `.opencode-state` paths. After `make opencode-recreate`, SSH into the new container, create/attach `awoki` again, and run `awoki-opencode` to attach to the restarted backend.

Use `<prefix> r` to reload and `<prefix> e` to edit the local tmux layer. Edits made inside the running container are lost on recreation; persist them in `.harness/config/tmux.conf.local` and rebuild.

Awoki retains the current path in new sessions/windows/panes, uses vi copy mode, keeps 100,000 history lines, and defaults **mouse mode off**. gpakosz/Oh my tmux! deliberately enters copy-mode when mouse selection is active, which makes Mac touchpad scrolling surprising in VS Code/SSH terminals. Toggle mouse mode with `<prefix> m` when needed.

For copy-mode use `<prefix> Enter`, then `v` to select and `y` to copy. Traditional tmux uses `Ctrl-b [` for copy-mode and `Ctrl-b ]` for pasting a buffer; `]` is not the copy-mode key. Awoki configures tmux `set-clipboard external`, so tmux copy operations can use OSC 52 to update the outside terminal/host clipboard without allowing arbitrary applications inside tmux to request clipboard writes through tmux. The gpakosz `tmux_conf_copy_to_os_clipboard` external-tool path remains disabled because a headless Linux container has no macOS `pbcopy` process.

VS Code added OSC 52 terminal clipboard support in 1.91. When using VS Code Remote-SSH, keep the local setting `terminal.integrated.enableOsc52=true`. `terminal.integrated.macOptionClickForcesSelection=true` is also useful when mouse mode is temporarily enabled because Option-drag can force normal terminal selection on macOS.

Verify the negotiated tmux clipboard capability inside tmux with:

```bash
tmux show -s set-clipboard
tmux info | grep 'Ms:'
```

The expected first value is `external`; `Ms` should not be reported as `[missing]`. The image build and entrypoint run `.harness/bin/tmux-check` as a startup smoke test; `make opencode-runtime-check` repeats the tmux and MCP checks against a running container.

## Awoki MCP startup

Awoki's server uses the MCP Python SDK 1.x `FastMCP` API. `requirements.txt` and `pyproject.toml` constrain it to `mcp>=1.29,<2`; build and startup preflights reject MCP 2.x or missing imports. For an OpenCode `Process exited with code 1` error, run `/awoki/.harness/bin/mcp-preflight` and inspect `/awoki/.harness/state/mcp-local.stderr.log`.

## Remote embeddings and reranking

Configure endpoint variables in `.env`. No local model cache is mounted or downloaded. Qdrant remains the semantic vector store.

Example embedding values:

```env
AWOKI_EMBEDDING_PROVIDER=openai
AWOKI_EMBEDDING_MODEL=text-embeddings-inference
AWOKI_EMBEDDING_DEPLOYMENT_ID=jinaai/jina-embeddings-v2-base-code
AWOKI_EMBEDDING_BASE_URL=http://embedding.example.invalid:8000/v1
AWOKI_EMBEDDING_API_KEY=
AWOKI_EMBEDDING_BATCH_SIZE=32
AWOKI_EMBEDDING_NORMALIZE=1
AWOKI_VECTOR_SIZE=768
AWOKI_QDRANT_COLLECTION=awoki_jina_embeddings_v2_base_code_768
AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH=0
```

A TEI deployment fixes the real embedding model as
`jinaai/jina-embeddings-v2-base-code`; `text-embeddings-inference` is the model
field used by its OpenAI-compatible API.

Native TEI reranking uses:

```env
AWOKI_RERANK_ENABLED=1
AWOKI_RERANK_PROVIDER=tei
AWOKI_RERANK_URL=http://reranker.example.invalid:8000/rerank
AWOKI_RERANK_MODEL=
AWOKI_RERANK_FAIL_MODE=fallback
```

These values are injected from the host `.env` when Docker Compose creates the container.
The repository `.dockerignore` excludes `.env` from the image, so `/awoki/.env` being absent
inside SSH is expected and avoids duplicating the dotenv secret file in the image. `sshd`
does not copy the container environment into login shells, so the root entrypoint writes
an allowlisted snapshot to `/run/awoki/runtime.env` and
`.harness/bin/mcp-auto` restores it immediately before starting the Awoki MCP
child. The snapshot is held on tmpfs, owned by `root` and the `op` account's
primary group, mode `0640`, replaced atomically, and is not included in a
repository mount, persistent volume, or backup. `mcp-auto` validates the default
snapshot and parent directory ownership/writeability before sourcing it.

An ordinary `ssh ... env` command therefore omits retrieval/Burp/Lavish settings;
that is intentional. Normal skills use MCP and must not `cat` or manually source
the runtime snapshot. Verify retrieval state with passive `retrieval_status`; use
`retrieval_probe` only when live Qdrant/embedding/reranker connectivity is required.

For deliberate shell-side diagnostics use the guarded runtime surface:

```bash
make runtime-config
make embedding-benchmark
make reranker-benchmark

# lower-level examples inside the SSH container
.harness/bin/awoki-runtime-env --profile qdrant -- curl -sS http://qdrant:6333/collections
.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py status
```

The Make targets work from the host checkout or from inside the SSH container. The host
launcher is intentionally compatible with macOS's system Bash 3.2, and hermetic host
validation does not require runtime-only retrieval SDK packages such as `httpx`/`openai`.
`runtime-config` redacts API keys, strips URL userinfo/query/fragment data, and prints the
runtime-snapshot mtime so operators can detect that `.env` changed after container creation.
Prefer it over pasting `docker compose config` output into diagnostics: Compose expansion
can include configured secret values.
`embedding-benchmark` and `reranker-benchmark` send fixed synthetic content only,
never repository source. They intentionally use one-shot standard-library HTTP with no retries or redirects to isolate endpoint latency/contract behavior; the embedding diagnostic reports the production retry settings separately. Benchmark arguments can be supplied explicitly, e.g.
`make embedding-benchmark EMBEDDING_BENCHMARK_ARGS='--batch-size 8 --timeout-seconds 180'`.

`awoki-runtime-env` starts children from a clean environment and provides only the
selected profile. `mcp-auto` itself relaunches the MCP server through the internal
`mcp` profile so stale SSH/OpenCode `PATH`, `PYTHON*`, `LD_*`, proxy, and unrelated
variables do not bleed into the secret-bearing server process:

```text
base       Awoki/Harness path + mode values
qdrant     base + Qdrant/code-search settings, no embedding/reranker API-key vars
retrieval  qdrant + embedding/reranker settings, including configured API keys
burp       base + Awoki Burp-adapter endpoint/timeouts, no retrieval API-key vars
lavish     base + Lavish port/version/state, no retrieval API-key vars
mcp        internal MCP launch profile: base + Qdrant/retrieval/Burp; secret-bearing
all        complete snapshot, including configured API keys
```

Endpoint URLs are passed verbatim to command profiles and should not contain
embedded credentials. Never run repository-controlled code, Git/build/test commands,
or downloaded tools under `retrieval`/internal `mcp`/`all`; those children can read the API keys.
Lavish deliberately uses the non-retrieval `lavish` profile before invoking `npx`.
Live Burp stays on direct `mcp.burp`; only Awoki archive/helper CLI uses `burp`.

The profile mechanism is an **accidental-inheritance control, not a sandbox**. The
current stdio MCP launcher runs as `op`, so `/run/awoki/runtime.env` is necessarily
readable by that same runtime user. A malicious process already executing as `op`
could deliberately read the snapshot even when its profile omitted those variables.
The same principle applies to other same-user OpenCode state that may contain provider
authentication. Treat the OpenCode SSH container as a trusted analysis runtime: inspect
untrusted repositories as data, do not execute their scripts/build hooks inside a
container that holds retrieval/OpenCode credentials, and use a separate credential-free
sandbox when target code itself must run.

Awoki also strips retrieval/provider credentials from its own repository-facing Git and
`rg` subprocesses and removes ambient `PYTHON*`, `LD_*`/`DYLD_*`, SSH-agent, and caller-selected
Git SSH helper variables before those tools launch. The deterministic Go semantics helper
uses a fixed credential-free environment and executes only Awoki's bundled/prebuilt probe,
never repository Go source. This is defense in depth against accidental propagation; it does
not change the same-user snapshot limitation above.

`AWOKI_RERANK_API_KEY_ENV` is an advanced local-process indirection. Stock Compose
can pass the *name* but cannot dynamically import an arbitrary host variable by that
name. In the standard Docker/OpenCode deployment set `AWOKI_RERANK_API_KEY` directly.
If a custom Compose override explicitly injects the named secret variable, the SSH
entrypoint validates the name and resolves it into the canonical key before snapshotting;
when reranking is enabled, a configured indirection whose named variable is absent
or empty fails startup instead of silently running an unauthenticated reranker request.
The retrieval backend independently applies the same fail-closed resolution in local
and non-SSH Docker modes, so a broken indirection never becomes an unauthenticated
network request. If reranking is disabled, the same stale indirection only emits an
SSH-startup warning.

Editing `.env` does not change a running container or an already-started MCP
process. Recreate `awoki-opencode-ssh`, start a new OpenCode process, and call
`retrieval_status`. A Bash `export` cannot reconfigure the existing MCP process.


The launcher checks Qdrant from inside `awoki-opencode-ssh` through
`http://qdrant:6333/readyz`; it does not wait on the host loopback endpoint.

## UID/GID build arguments

`AWOKI_HOST_UID` and `AWOKI_HOST_GID` set the numeric Linux identity of the
non-root `op` user when the image is built. They do not synchronize host account
or group databases. On macOS Docker Desktop, leaving them unset uses Compose's
`1000:1000` defaults and normally works. Matching macOS values such as `501:20`
is usually unnecessary and can collide with existing Debian group numbers.

Changing the values requires an image rebuild. Existing named-volume files keep
their previous numeric ownership and may require a deliberate ownership migration.
Do not configure UID or GID `0` for the normal OpenCode process.

## Stop

```bash
make opencode-ssh-down
```


## Ad-hoc Lavish

Invoke `/lavish` only when visual review is useful. Lavish runs inside the SSH container on a pinned Node 22 runtime, binds to the container interface required for Docker forwarding, and is published only at host loopback `127.0.0.1:${AWOKI_LAVISH_PORT:-4387}`. The skill executes it through the `lavish` runtime profile, so customized port/version/state values survive SSH without placing retrieval API keys in `npx`'s environment. It starts with `--no-open`; from macOS run `./open-lavish.sh`. No SSH tunnel, ambient hook, or external share is required.

## Backup implications

Default Awoki backups do not include `.opencode-state`, `.ssh-container`, Neovim named volumes, or the SSH server host-key named volume. Use `BACKUP_INCLUDE_OPENCODE_STATE=1` only when OpenCode session/config state is explicitly required, and `BACKUP_INCLUDE_SECRETS=1` only when `.env` and the SSH client key must move. Caches remain excluded. Server host keys and Neovim state are recreated on the new installation. See `docs/BACKUP_RESTORE.md`.

## OpenTUI `/tmp` requirement

OpenCode's OpenTUI renderer extracts a native shared library into `/tmp`. Docker-created tmpfs mounts default to `noexec`, which prevents the dynamic loader from mapping executable segments and produces an error like:

```text
Failed to initialize OpenTUI render library
failed to map segment from shared object
```

Awoki therefore leaves `/tmp` on the container image writable layer and verifies executable access before starting SSH. `/awoki/.harness/tmp` and `/run` remain tmpfs mounts. After upgrading from a release that mounted `/tmp` as tmpfs, recreate the container rather than only restarting it:

```bash
docker compose -f docker-compose.opencode.yml down
docker compose -f docker-compose.opencode.yml build --pull --no-cache awoki-opencode-ssh
./run-opencode.sh
```
