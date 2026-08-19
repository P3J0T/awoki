# Awoki Operator Reference

> Dense operational reference preserved from the pre-R9.1.6.16 README. Start with [`../README.md`](../README.md) for the public overview and [`AWOKI_IDENTITY.md`](AWOKI_IDENTITY.md) for the maintainer/context identity.


Awoki is a Docker-first, continuity-first OpenCode harness for scoped project/global memory, exact and semantic retrieval, safe evidence summaries, skills, and resumable security or engineering work.

## Core properties

- Named projects live under the Git-ignored `workspace/projects/<project_id>/` tree. A project may keep the legacy single repository at `repo/` or register multiple exact roots under `repo/<repo-id>/`.
- Canonical project continuity is append-only `memory/continuity.jsonl`.
- `SITUATION.md` and `HANDOFF.md` are generated bounded views.
- SQLite FTS provides local lexical retrieval; Qdrant stores derived semantic vectors.
- Embeddings come from a configured remote OpenAI-compatible endpoint. No embedding or reranking model weights are stored in the Awoki container.
- Remote reranking is optional and independently configurable.
- `/codebase` provides native structural repository search with symbol definitions, branch-aware exact/FTS/vector retrieval, call graphs, and bounded results. Exhaustive local text search is coverage-first: parser support, auth/security vocabulary, and security-named paths do not define the searchable universe; obvious raw-secret/config files are kept out of embeddings/structural indexes but may be accounted for locally with opaque previews.
- Portable/full runtime backups provide checksum-verified migration with fail-closed restore.
- Direct PortSwigger Burp MCP remains the live Burp control plane.
- One authenticated OpenCode Web/server backend runs inside the SSH container by default; browser and SSH TUI share it through `opencode attach`.
- Awoki source is baked into the image; only explicit runtime trust domains are writable.
- Neovim is minimal; tmux uses a vendored, integrity-recorded gpakosz/Oh my tmux! configuration with an Awoki-owned local override.
- There is no built-in credential database or credential MCP surface.
- Explicit user-directed sensitive memory is supported as secret/no-RAG data and is excluded from generated views and automatic retrieval.

## Fresh installation

Start from a writable Awoki Git checkout. Do not run raw `docker compose up` as the
normal first-start path: the supported bootstrap prepares host SSH material before
Compose is allowed to inspect its bind sources.

```bash
cp .env.example .env
# Configure AWOKI_EMBEDDING_* and, if used, AWOKI_RERANK_* before semantic indexing.

make dev-preflight
./init-awoki.sh
make doctor
make dependencies-check
make validate
make install-opencode-ssh
make opencode-runtime-check
```

`make opencode-runtime-check` refreshes the root-owned `/run/awoki/runtime.env`
snapshot before validating the `op` runtime. A missing snapshot after container
installation is therefore repaired by the check instead of producing a stale
bootstrap false negative.

`make install-opencode-ssh` builds the image and reaches the guarded
`opencode-ssh-up` launcher. For later starts, when the image is already built, use:

```bash
make opencode-ssh-up
make opencode-runtime-check
```

Connect:

```bash
make opencode-web-password
# Browser: http://127.0.0.1:${AWOKI_OPENCODE_WEB_PORT:-4096}
ssh -i .ssh-container/id_ed25519 -p ${AWOKI_OPENCODE_SSH_PORT:-2222} op@127.0.0.1
cd /awoki
awoki-opencode
```

`./init-awoki.sh` creates the host SSH client key pair. `./run-opencode.sh`
(used by `make opencode-ssh-up`) revalidates/synchronizes that pair, derives the
public key, exports only that public value to Compose, starts Qdrant and the
OpenCode container, checks Qdrant through the internal Docker network at
`http://qdrant:6333/readyz`, verifies SSH, and prints the connection command. The
container validates the injected public key and installs
`/home/op/.ssh/authorized_keys` itself. No `.ssh-container/*` file is bind-mounted
into the container, avoiding Docker Desktop's macOS single-file `/host_mnt` bind
failure while keeping the private key host-only. Initialization also creates the
bind-mounted `data/qdrant/collections/` parent required for reliable collection
creation. The same launcher secures host `.opencode-state/` and `.opencode-state/web-auth/`
to `0700`, prepares `.opencode-state/web-auth/password` as a single-link host-only
`0600` secret, mounts its directory read-only, and the entrypoint copies it to
`/run/awoki/opencode-web-password` as `op:op 0600`. The password is not in
the Compose service environment or `/run/awoki/runtime.env`; OpenCode receives
it only through the Web/attach process environment because its Basic-Auth server
requires the original password rather than a hash.

### First managed project: `test2` with Oathkeeper

Awoki project creation and repository registration are separate from cloning. Create
the project first, clone the repository into the project's controlled `repo/`
container, then register that exact Git root. The host CLI below uses the same project
workspace rules as MCP and does not index or embed repository code.

```bash
# From the writable Awoki checkout.
python3 .harness/project.py create test2

git clone \
  https://github.com/ory/oathkeeper.git \
  workspace/projects/test2/repo/oathkeeper

python3 .harness/project.py repo-add \
  --default \
  test2 \
  oathkeeper \
  repo/oathkeeper
```

For a reproducible retrieval benchmark, pin the target repository to an explicit
commit before indexing and record that commit in the report. Do not silently update
the checkout between acceptance runs. For example:

```bash
git -C workspace/projects/test2/repo/oathkeeper rev-parse HEAD
# Optional benchmark pin:
# git -C workspace/projects/test2/repo/oathkeeper checkout --detach <commit-sha>
```

Open or resume the project in a fresh OpenCode session before repository analysis:

```text
project_open(name="test2", create_if_missing=false)
project_repo_list(name="test2")
```

Freshly cloned repositories do not yet have Awoki's derived structural or semantic
indexes. Materialize them explicitly. Local structural/FTS indexing is detached and
credential-free:

```text
code_index_refresh_start(name="test2", repo="oathkeeper")
```

The start call returns a `cir_...` job immediately. Do not run a tight polling loop.
When the user asks for progress, or after the returned recommended interval, check the
same job once:

```text
code_index_refresh_status(
  name="test2",
  repo="oathkeeper",
  job_id="<cir-job-id>"
)
```

For normal repository preparation, prefer the durable parent readiness job rather
than manually chaining structural and vector workers:

```text
repository_prepare_start(
  name="test2",
  repo="oathkeeper",
  mode="full",
  resume_goal="continue the requested review after FULL_READY"
)
```

`mode="full"` is explicit semantic-materialization authorization for that exact
managed source. The parent job owns structural refresh/verification, vector
materialization, exact published-membership verification, and bounded live
Qdrant/embedding/reranker probes. It keeps advancing while OpenCode is idle or closed;
no model turn is required between phases. Check it later with the returned `rpr_...`
job id:

```text
repository_prepare_status(
  name="test2",
  repo="oathkeeper",
  job_id="<rpr-job-id>"
)
```

Use `mode="local"` when remote embedding has not been authorized. `LOCAL_READY` means
structural/FTS review is current. `FULL_READY` additionally means the exact vector
membership is current and the configured Qdrant, embedding, and reranker backends have
passed readiness probes. A failed or partial vector population never satisfies
`FULL_READY`.

The lower-level `code_index_refresh_*` and `code_vector_refresh_*` tools remain escape
hatches and diagnostics. The vector worker persists successful content-addressed
batches incrementally and performs bounded retries for transient connection/rate-limit/
selected HTTP failures. Timeout/request-capacity failures on oversized batches are
adaptively split instead of repeatedly replaying the same large request, and successful
reduced sub-batches are persisted immediately. Authentication,
configuration, malformed-response, and vector-dimension failures are not treated as
retryable transport errors. If retries are exhausted, the parent stops as blocked;
already persisted vectors remain reusable by a later explicit preparation attempt.
Awoki never publishes incomplete vector membership as full readiness.

OpenCode TODO is a visible projection of this parent job. Optional
`project_continuation_*` state can best-effort resume an originating conversation after
a detached job reaches a terminal transition, but conversation wake-up is not part of
repository-readiness correctness. This self-resume path is deliberately narrow: it uses
one-shot timers, a lease, a 48-hour lifetime enforced both while polling and again at
claim time, and at most three auto-resume claims across one active continuation chain.
Advancing an unfinished chain preserves both its deadline and consumed claim count; a
new explicitly scheduled workflow after terminal finalize/cancel/block starts a new bound.
It never turns a failed/blocked readiness job into an unbounded retry loop. An explicitly
named existing managed project can be prepared even when no project is attached to the
current OpenCode session. If another project is attached, optional continuation must not
silently switch it. True ad-hoc paths are rejected with `MANAGED_SCOPE_REQUIRED`; they
are not silently promoted into persistent project or vector state.

Generic model-turn recovery is separate. The continuity plugin observes structural
OpenCode message metadata and records a `reasoning_only_terminal_turn` when an assistant
turn finishes with reasoning present but no normal text/tool part. It separately records
`tool_execution_without_followup` when an executable tool completed but the terminal
assistant turn never produced normal text continuation. Query `session_runtime_status`
for the last terminal turn, anomaly count, manual recovery attempts, finish reason,
structural provider/model/agent identity, any provider error class, completed-tool count,
step-finish token counters, and the durable compaction generation when available. Awoki
never persists the reasoning text and does not automatically send a
`continue` prompt for generic model failures. A user follow-up after such an anomaly is
counted in `agent_turn_recovery_attempts`; it does not consume the epistemic
`corrective_budget`.

Project `reports/` is created with the project layout and is the intended location for
saved human acceptance reports. OpenCode's native TODO list is mirrored into bounded
`.harness/state/work-ledger/` operational state with Awoki-owned stable `atd_` IDs so
compaction can preserve the active plan even for unattached/ad-hoc sessions; newer user
direction always overrides an older mirror. High-confidence credential values are
redacted before TODO state is persisted.

Multi-test acceptance runs use two persistence planes. `acceptance_run_*` v4 stores
compact typed per-test observations/invariants under project `artifacts/acceptance/`,
bound to the source revision and published vector membership captured at run start. A
bounded `test_plan` can preserve the parts of an acceptance contract that must survive
automatic/manual compaction: required execution MCP interfaces, required acceptance
orchestration interfaces, required scalar observations, machine-checkable scalar pass
requirements, current-run evidence scope/minimum refs, native-tool allowlists/count
limits, per-interface execution/orchestration call ceilings, forbidden tool classes, action labels, and stop boundaries. This is workflow/protocol structure, not a schema for analytical content.
`acceptance_run_next` returns the first unfinished planned test plus its exact durable
contract and already-observed structural provenance after each record/compaction, which
keeps long suites from rebuilding their scheduler or PASS criteria from model memory.
Execution provenance and acceptance-orchestration provenance are separate: normal work
can require tools such as `session_runtime_status`, while scheduler recovery can require
`acceptance_run_status`/`acceptance_run_next` without making those calls invisible or
letting `acceptance_run_record` prove its own test. The OpenCode plugin records only tool
name/class/start/completion for the active test—never arguments, output, source, or
reasoning. Awoki MCP names are canonicalized before protocol checks, so provider-exposed
names such as `awoki_retrieval_status` count as the canonical `retrieval_status` MCP
interface rather than a native command. `acceptance_run_record` machine-checks
those observable conditions and may downgrade a claimed PASS to `incomplete` or
`protocol_deviation`; the model can no longer make an out-of-contract test look passed
merely by writing PASS. Allowed/forbidden action entries remain suite labels only, never
new authorization; normal Awoki/tool policy remains authoritative. The acceptance ledger
does not own the reliability corrective budget. When rich support may be needed
later, `codebase_search(capture_evidence=true,
acceptance_run_id=...)` writes the exact Awoki-produced result (and metadata-only deep
diagnostic trace) to a content-addressed `artifacts/acceptance/raw/evidence/ev_...`
artifact and returns canonical `cand_...` candidate IDs. The compact ledger references
those IDs; a bounded non-RAG capture-provenance sidecar records which acceptance runs
actually captured a stable content-addressed `ev_`, so `evidence_scope=current_acceptance_run`
can be enforced without changing evidence identity. `acceptance_evidence_get` retrieves
bounded evidence slices after compaction without rerunning retrieval. Raw evidence
artifacts are integrity-checked and never registered for RAG. Compaction generation/count
and a bounded event history are persisted with the run, so earlier automatic compactions
remain visible after later ones. Compaction context reinjects a small core execution
contract: Awoki operation names remain MCP interfaces, native Read/Bash/grep is not an
MCP fallback, and `acceptance_run_next` is authoritative for the active test. Incomplete
finalization is rejected and remains resumable. Final reports should aggregate the ledger
rather than reconstruct exact prior ranks/scores from compacted chat memory. Persisted
observations are continuity aids, not independent machine proof.


Stable machine IDs remain authoritative, but Awoki also exposes a human navigation layer.
`reference_describe(id)` explains what a durable `acr_`, `ev_`, `cand_`, `vrf_`, `rel_`,
`asn_`, `cont_`, or session `atd_` reference represents, why it is retained, its bounded
origin/scope, and linked refs without dumping rich payloads. `reference_annotate` may add
a human `label`, `why_saved`, aliases, and links without changing the stable ID or its
provenance. `reference_resolve("the bearer-token reranker evidence")` returns bounded
candidate stable IDs for navigation; natural-language matching is never authoritative.
This metadata lives in non-RAG control-plane state, and compaction carries a tiny active
reference map so later turns can recover semantic names without replacing exact IDs.
Long explanations do not belong in acceptance `notes`: notes remain <=800 characters and
<=4 newlines; machine facts belong in observations, rich support in `ev_...`, and human
context in reference labels/`why_saved`.

R9.1.6.14 hardens that navigation boundary further. Natural-language resolution now reports `resolved`, `ambiguous`, `not_found`, or exact-ID resolution; ambiguous/low-confidence phrases intentionally return no `resolved_reference_id`. Candidate descriptions expose project-local `first_materialized_in`, bounded `observed_in` evidence occurrences, and an exact occurrence count so a stable `cand_` identity is not mistaken for belonging to only one retrieval artifact. Acceptance v4 also retains immutable bounded `aat_` attempt history, allowing a first machine `INCOMPLETE` and a later bookkeeping correction to remain visible simultaneously. Optional `interface_limits` / `orchestration_interface_limits` can cap repeated MCP calls without treating those calls as native tools.

R9.1.6.15 keeps that v4 contract backward-compatible and makes bookkeeping corrections easier to drive without self-referential acceptance criteria. `acceptance_run_record` now returns a bounded attempt summary for the record it just created plus the immediately prior attempt context (`prior_attempt_count`, prior attempt ID, claimed/effective outcome). A test contract may also declare `prior_attempt_requirements`, which are evaluated against immutable machine-owned attempt history (`count`, `exists`, and the immediately prior attempt's ID/number/claimed/effective outcome) before the new attempt is written. A correction drill can therefore require “one prior attempt exists and it was machine-INCOMPLETE” without predicting the not-yet-computed outcome of the current attempt. The response summary is convenience metadata; the durable history and machine-evaluated prior-attempt context remain authoritative.

Compaction history is similarly explicit: OpenCode's structural compaction part is used when available to record `automatic_context_pressure` versus `explicit_request`; older/unsupported runtimes remain `unknown` rather than being guessed from timing. The generation/count remain authoritative for continuity, while the trigger explains why each generation occurred.

For Awoki's own acceptance regressions that need executable harness checks, use
`harness_self_check`. It exposes only curated allow-listed checks such as
`compaction_acceptance_boundaries` and `detached_self_resume_bounds`; it is intentionally not a generic shell/pytest runner.
This lets long acceptance suites stay MCP-mediated after compaction instead of rediscovering
test files with native Read/grep/Bash and accidentally broadening the requested test.

OpenCode conversational-model authentication is separate from Awoki embeddings. Configure the OpenCode provider with `opencode auth login`; the `AWOKI_EMBEDDING_*` variables only control retrieval vectorization.

OpenCode's OpenTUI renderer requires executable `/tmp`; Awoki does not mount Docker's default `noexec` tmpfs over `/tmp` and checks this invariant at container startup.

Awoki currently uses the maintained MCP Python SDK 1.x API and constrains the dependency to `mcp>=1.29,<2`. MCP SDK 2.x is a breaking API line and is rejected during image build and container startup instead of surfacing later as an unexplained OpenCode MCP process exit.

### OpenCode runtime policy and critical dependencies

Awoki no longer hard-pins OpenCode to a release baked into the source tree. Fresh
OpenCode-image builds default to **latest / untested**: the build resolves the current
`opencode-ai@latest`, then materializes `@opencode-ai/plugin` and `@opencode-ai/sdk` at
that exact resolved CLI version. A compatibility gate verifies all three versions agree
and writes the immutable image record to
`/usr/local/share/awoki/opencode-runtime.json`. OpenCode auto-update remains disabled
inside the running container, so a container never changes underneath an active session.
Awoki does not separately impose version ceilings on OpenCode's internal AI/provider or
other transitive dependencies; those are resolved from the selected OpenCode release.
Only the local Awoki plugin/SDK API surface is deliberately aligned to the resolved CLI
release so a mismatched interface fails during image construction instead of mid-session.

The default latest build is intentionally a moving dependency policy, not a promise that
every newly published OpenCode release has already been accepted by Awoki. Use the
normal validation/recreation path to promote a resolved version through your own tests.
If a new release regresses agent behavior, use **safe mode** with any exact version you
choose as the last known good:

```bash
# Default: resolve current latest during this fresh build.
make opencode-recreate OPENCODE_RECREATE_ARGS="--validate --no-cache --opencode-latest"

# Safe mode: exact operator-selected OpenCode CLI/plugin/SDK version.
make opencode-recreate OPENCODE_RECREATE_ARGS="--validate --no-cache --opencode-safe <last-known-good-version>"

# Safe mode during the first SSH install instead of recreation.
make install-opencode-ssh OPENCODE_INSTALL_MODE=safe OPENCODE_SAFE_VERSION=<last-known-good-version>
```

`make opencode-recreate` defaults to latest mode and injects a fresh resolver token so
Docker cannot silently reuse an old `npm install opencode-ai@latest` layer. Source
`.opencode/package.json` also follows `latest` for development; safe image builds rewrite
the baked config-directory dependencies to the exact resolved version before runtime.
The image build and SSH entrypoint both verify the manifest against the actual CLI,
plugin, and SDK; `make opencode-runtime-check` repeats the same compatibility gate on
demand. A normal later `make opencode-ssh-up` does not run the package resolver, so
starting an existing image cannot silently move it to a newer OpenCode release. Do not
run `opencode upgrade` inside the container and treat that mutation as an Awoki release: it is intentionally non-durable and bypasses the compatibility record.

`.harness/runtime-dependencies.lock.json` records this OpenCode **policy** rather than an
OpenCode version ceiling. Other critical defaults remain explicit there: the Lavish
default, Qdrant image, builder/runtime images, Go semantics toolchain, and direct Python
requirements. `make dependencies-check` verifies that those contracts and the dynamic
OpenCode latest/safe machinery agree with Docker, Compose, and the local plugin package
manifest.

When changing runtime/dependency policy or other critical dependencies:

1. Change `.harness/runtime-dependencies.lock.json` and every referenced build/runtime
   file in the same reviewed commit. For OpenCode, change the policy/compatibility gate
   rather than adding another hardcoded version. For Lavish and Qdrant, update their
   reviewed defaults together with the corresponding Compose/skill configuration.
2. Run `make dependencies-check` and `make validate`.
3. Rebuild and force-recreate the baked OpenCode runtime. Dependency/base-image changes
   should use `--no-cache`; latest OpenCode recreation already forces the resolver layer
   to re-evaluate the package channel.
4. Run the repository-readiness/continuation and agent-runtime acceptance workflow before
   treating the newly resolved stack as known good. If it is not good, rebuild with
   `--opencode-safe <exact-version>` rather than mutating the live container.

### Run administrative commands as root

The SSH `op` account has no usable password, no `sudo` command, and no sudo access. There is therefore no sudo password to enter. For deliberate container administration, run Docker Compose from the host and select UID 0 explicitly:

```bash
docker compose -f docker-compose.opencode.yml \
  exec -u root awoki-opencode-ssh bash
```

For a single root command without opening an interactive shell:

```bash
docker compose -f docker-compose.opencode.yml \
  exec -u root awoki-opencode-ssh sh -lc '<command>'
```

Root can modify every writable path mounted into the container, so use it only for targeted maintenance. Changes made to the image filesystem are lost when the container is recreated; permanent package or image changes belong in `Dockerfile.opencode` followed by an image rebuild.

The baked `/awoki` runtime tree is intentionally not an Awoki development
checkout. It may have no top-level `.git` and may be read-only to `op`. An agent
asked to modify Awoki itself must first run `make dev-preflight` (or `.harness/bin/awoki-dev-preflight`) and require it to pass; the preflight verifies a writable top-level Awoki Git
checkout (`.git`, matching `git rev-parse --show-toplevel`, writable target
files). If that check fails, it must stop rather than trying `sudo`, `su`,
`chown`, `chmod`, `/root` discovery, or ownership workarounds. Develop in the host
checkout or a separate writable dev clone, then rebuild/recreate the runtime. Because `/awoki` source is baked into the OpenCode image, use the reviewed helper after source/plugin/Docker/Compose changes:

```bash
make opencode-recreate
# Add full validation when desired:
make opencode-recreate OPENCODE_RECREATE_ARGS="--validate"
# Dependency/base-image changes should also bypass Docker cache:
make opencode-recreate OPENCODE_RECREATE_ARGS="--validate --no-cache --opencode-latest"
# Roll back/test an exact known-good OpenCode stack:
make opencode-recreate OPENCODE_RECREATE_ARGS="--validate --no-cache --opencode-safe <last-known-good-version>"
```

The helper force-recreates only `awoki-opencode-ssh`; it does not force-recreate Qdrant. It also warns, without editing `.env`, when an old copied `AWOKI_CODE_RERANK_TIMEOUT_SECONDS=5` setting is still present.

### tmux

The OpenCode image vendors [gpakosz/.tmux](https://github.com/gpakosz/.tmux) under `/opt/oh-my-tmux`; it never downloads or executes the upstream installer at container startup. The immutable upstream configuration is linked as `/home/op/.tmux.conf`, while Awoki customizations live in `/home/op/.tmux.conf.local`. The vendored licenses, retrieval date, and SHA-256 hashes are recorded in `.harness/vendor/oh-my-tmux/UPSTREAM.md`.

Start or reattach the standard session:

```bash
tmux new -A -s awoki
```

Both `Ctrl-b` and `Ctrl-a` work as prefixes. Useful bindings include `<prefix> r` to reload, `<prefix> e` to edit the local override, `<prefix> -` and `<prefix> _` to split panes, and Vim-style `h/j/k/l` pane navigation. Awoki enables mouse support, vi copy mode, 100,000 lines of history, current-path retention, and disables automatic host clipboard integration. Edits made with `<prefix> e` affect the running container only and are lost when it is recreated; make persistent changes in `.harness/config/tmux.conf.local` and rebuild the image. The image build and SSH entrypoint both execute `.harness/bin/tmux-check` as a startup smoke test; after startup, verify both tmux and Awoki MCP with:

```bash
make opencode-runtime-check
```

`make doctor` resolves configuration from the current shell first, then Docker Compose's resolved environment, then the repository `.env`. A configured endpoint is reported without printing the API key.

Docker Compose injects host `.env` values when the container is created. `.dockerignore`
excludes `.env` from the image, so `/awoki/.env` is intentionally absent and must not be
used as the runtime configuration source. Editing host `.env` does not mutate a running
container or an already-started Awoki MCP process. After retrieval configuration changes,
recreate the service and start a new OpenCode process; a Bash `export` inside an agent tool
call affects only that one shell.

The SSH login shell intentionally does not inherit the container's Compose environment.
Awoki MCP restores an allowlisted root-owned tmpfs snapshot immediately before launch.
For explicit diagnostics, do not `cat` or manually source that snapshot. Use:

```bash
make runtime-config
make embedding-benchmark
make reranker-benchmark
```

Those Make targets work from the macOS/host checkout or from inside the SSH container.
The host-side wrapper remains compatible with the macOS system Bash 3.2; host validation
does not require container-only retrieval SDK packages such as `httpx` or `openai`.
`runtime-config` redacts API keys, strips URL userinfo/query/fragment data, and reports the
runtime-snapshot mtime so a stale running container is obvious; prefer it over sharing
`docker compose config`, whose expanded environment may contain secrets.
Backend benchmarks send fixed synthetic text only. They use one-shot Python standard-library HTTP requests with no retry/redirect behavior so observed latency reflects the backend request itself; configured production retry limits are reported but not replayed by the benchmark. The lower-level `awoki-runtime-env` wrapper
uses profile-filtered environments: `qdrant`, `burp`, and `lavish` exclude retrieval API-key
variables; `retrieval`, internal `mcp`, and `all` are secret-bearing. The normal MCP path
relaunches the server through the internal clean `mcp` profile; operator diagnostics should
normally use `qdrant`, `retrieval`, `burp`, or `lavish` as appropriate.
Never use a secret-bearing profile around repository-controlled code, Git/build/test
commands, or downloaded tools. Normal agent work should use MCP rather than shell env.
Profiles reduce accidental environment inheritance; they are not a same-user sandbox.
Because the stdio MCP launcher itself runs as `op`, that runtime user can deliberately
read the tmpfs snapshot. Do not execute untrusted target code in a credential-bearing
OpenCode container; use a separate credential-free execution sandbox for that case.
Awoki's own repository-facing subprocesses are narrower than the MCP environment: passive
Git reads and exhaustive `rg` scans strip retrieval/provider credentials and ambient
loader/interpreter/SSH-agent overrides before launch, while the deterministic Go semantics
helper already runs with its own fixed credential-free environment. This reduces accidental
credential propagation into inspection tools, but it still does not defeat malicious
same-user code that deliberately opens `/run/awoki/runtime.env`.

## OpenCode interface

```text
AGENTS.md                       concise always-loaded behavior invariants
HARNESS.md                      human-readable harness map
opencode.jsonc                  project OpenCode/MCP configuration
.opencode/plugins/              session continuity and compaction injection
.opencode/skills/               on-demand procedures
.opencode/commands/             slash commands
.harness/server.py              Awoki MCP
```

Preferred continuity tools:

```text
project_open
project_capture
project_search
project_refresh
project_pause
project_status
```

Repository membership is also natural-language driven through `project_repo_add`,
`project_repo_list`, `project_repo_remove`, and `project_repo_default`. For the
common layout `repo/oathkeeper/.git`, `/project add repo oathkeeper` infers
`repo/oathkeeper`, verifies that the child is the exact Git top-level when it is
Git-backed, and registers it without cloning, deleting, or moving files.

Important slash commands:

```text
/project              natural-language project create/open/refresh/capture/pause front door
/codebase             natural-language structural repository search
/definition           exact symbol definition lookup
/callers /callees     conservative static call relationships
/code-path            bounded resolved function path
/code-across          explicit cross-project repository search
/code-validate-claim  natural-language verification orchestrator over strict AST/graph proof
/code-index-status    structural index diagnostics
/burp                 natural-language live Burp and saved-evidence front door
/burp-repeater        explicit copy to Repeater without sending
/burp-intruder        explicit stage in Intruder without launching
/burp-send            explicit one-request network send
/burp-status          read-only Burp connectivity/status
/burp-validate        read-only Burp/Awoki integration validation
/retrieval-status     embedding/Qdrant/reranker diagnostics
/recall               project-aware recall
/project-status       project and code-index freshness
/explore /verify      investigation and focused evidence review
/reliability-check    local completion or reliable-pause gate
/ship-check           explicit delivery gate
/lavish               ad-hoc visual artifact review
/backup               portable/full runtime backup workflow
```

Natural language is the default. Result depth for `/codebase` is requested in words such as “locations only” or “show the full implementation”; separate peek/context/full aliases are not needed. Burp archive helpers and low-level MCP operations remain available internally without becoming slash-command clutter. See `docs/COMMANDS.md`.

### Multi-repository projects and semantic readiness

A project can contain several independently versioned repositories:

```text
workspace/projects/test2/
└── repo/
    ├── oathkeeper/
    │   └── .git/
    ├── hydra/
    │   └── .git/
    └── keto/
        └── .git/
```

`repo/` is the project Git-repository container in registered mode; every child is
its own provenance/index namespace. Non-Git textual evidence can additionally be
registered below `sources/<source-id>/` with `project_source_add`; current supported
corpus types include directory/corpus text, Smali, assembly text, and pseudocode text.
Smali has deterministic class/method/field/call/reference parsing and participates in
the same FTS/graph/Qdrant/reranker pipeline. Non-Git revisions are canonical manifests
of sorted relative path, byte size, and SHA-256, so identity is independent of mtime,
inode, and copy order. Broad `codebase_search` spans enabled evidence sources. Exact
operations use `repo=` for Git compatibility or `source_id=` when scope is ambiguous.
Legacy projects whose `repo/` itself is the exact Git root continue to work.

Opening a project and registering a repository are passive with respect to remote
embeddings. Their `repository_index_advice` reports structural/FTS and vector
freshness per repository. If an existing local structural snapshot is stale, OpenCode
should use the returned `code_index_refresh_start` action first. Local structural/FTS
reindexing now runs in a detached worker, returns a `cir_...` job immediately, exposes
bounded file/parser/current-path progress through `code_index_refresh_status`, and
can be cancelled explicitly with `code_index_refresh_cancel`; it performs no remote
embedding or Qdrant writes. `refresh_index=true` on MCP `codebase_search` is a
compatibility trigger for the same detached job rather than a synchronous full-repo
parse, so a large forced reindex cannot consume the OpenCode MCP request deadline.
If semantic vectors are missing or stale after structural refresh, direct
`code_vector_refresh_start` remains available as an explicit low-level action, but
normal prime/warm workflows should use `repository_prepare_start`. The parent readiness
worker owns the entire structural -> vector -> verification sequence without model
polling. It never uploads/embed source merely because a project/source was opened or
registered; full semantic preparation must be explicitly requested for one exact
managed `repo=` or `source_id=`. Non-Git semantic materialization still requires
explicit managed source scope.

The vector worker preflights Qdrant before the first embedding request, persists
completed content-addressed vectors incrementally, and reuses them across later runs.
Transient embedding transport failures receive bounded worker-local retry/backoff.
Timeout/request-capacity failures on batches larger than the configured adaptive minimum
can reduce the request size instead of repeatedly replaying the same large request;
rate-limit/server/connection failures do not fan out into more requests. Successful
reduced sub-batches are persisted immediately, so a later timeout
does not discard recovered work. Permanent authentication/configuration/protocol/
dimension failures are not automatically retried. Exhausted transient failures block
the parent preparation job and preserve truthful reusable/remaining counts. Partial
membership is never promoted to `FULL_READY`.

`repository_prepare_status` exposes bounded parent and active-child progress without
source text. `repository_prepare_cancel` is explicit-user-only and cancels the active
child owned by that parent. Direct child job status calls retain their bounded polling
cadence and remain useful for diagnostics. Optional OpenCode continuation may monitor
a parent job and best-effort resume the original conversation on terminal transition,
but the parent job itself is the durable source of truth and completes independently
of model/session lifecycle.

Managed source understanding is evidence-backed by default. A request such as
“explain how this input is processed” first uses indexed search to discover the
implementation, then exact symbols, a bounded relevant structural flow graph,
and hash-checked bounded source windows. Semantic/vector similarity can locate
candidate code but is never itself behavioral proof. Strict
`code_validate_claim` checks are used selectively for supported atomic
propositions underneath a broader investigation. Exact source windows carry
checksum-protected `evidence_id` tokens so later steps can call
`code_evidence_verify` and detect source/snapshot drift without treating the
token as a signature or authorship proof. Git provenance is reported as
`VERIFIED_SNAPSHOT`, `WORKING_TREE_BOUND`, or `FILESYSTEM_BOUND`; non-Git corpora
report `CONTENT_MANIFEST_BOUND`. Lower assurance never removes readable source.

Supported deterministic Go primitives can be observed with
`code_semantics_check` instead of guessed: path join/clean, duration
parsing/multiplication, failed `error` type assertion, bounded string replacement,
URL parsing, and `httputil.ReverseProxy` Rewrite-entry forwarded-header behavior.
Release Docker images compile one fixed stdlib-only helper with Go 1.26.5 in the
pinned `golang:1.26.5-bookworm` builder stage and copy only that small helper into
the runtime image; the Go compiler/toolchain is not shipped just for semantics
checks. The helper has no repository-code execution or network path. Source-tree
host validation may fall back to compiling that same fixed helper with the local
Go toolchain and reports local/project toolchain alignment rather than pretending
a version-mismatched stdlib observation proves the target runtime.

For conceptual/architectural questions, Awoki indexed/structural discovery is the preferred first move.
For ordinary known string/symbol lookup, OpenCode Grep is a normal exact-search tool;
for complex or exhaustive exact enumeration, `code_exact_search` is the first-class structured ripgrep path
when multiple expressions/counts/context/globs materially help. It does not need semantic
retrieval to fail first when the task itself is exact enumeration. If structured
exact-search output errors, truncates, hits a giant-line/client transport limit, or
cannot establish the coverage required for a claim, `code_text_search` becomes the
deterministic coverage path: it scans every source file allowed by explicit hard repository policy,
materializes the match universe once, and bounds only returned pages/previews. Normal source code
that handles tokens, passwords, JWT, OAuth, or credentials stays searchable; only high-confidence
literal secret values are redacted from derived/indexed/returned source text.
For a clean Git commit it reuses the matching code-index policy manifest instead
of redundantly hashing every file. If one call reaches the search operation
deadline it returns resumable `scan_complete=false` state rather than hanging;
later calls continue the same materialized search.
The internal `.harness/bin/code-search-fallback` helper is the MCP-unavailable
diagnostic equivalent and supports multiple roots plus pagination. Git repositories honor `.gitignore` by default; explicit forensic lexical searches can set `include_ignored=true` (CLI: `--include-ignored`).

Project direction remains natural-language driven; tasks and pending items are optional.

## Retrieval scopes

Awoki deliberately separates three related jobs:

```text
/codebase or codebase_search
  repository code only

project_search
  all safe indexed project material, project-first, with optional labeled global knowledge

memory reconciliation during project_capture
  prior continuity records only
```

This prevents repository chunks from being mistaken for previous memories. Repository source lives only in the dedicated structural code index; general project RAG continues to cover continuity, views, notes, reports, and approved artifacts. Code hits are annotated with a `source_role` (`production`, `test`, `test_fixture`, `config_schema`, `documentation`, or `generated_or_vendor`) plus a finer `authority_class`. Tests/config/schema remain searchable and useful for contracts, configuration, and edge cases; ordinary runtime/implementation questions prefer relevant concrete production functions/methods without hard-filtering corroborating evidence. Coarse production module/file hits are not treated as implementations: R9.1.3 refines strong containers into bounded concrete symbols using exact structural children plus an exact-file fallback for languages such as Go where receiver methods need not appear below the coarse container in the same hierarchy. Each child is independently reranked against the original query. If a concrete child was already present in broad discovery, refinement requalifies that exact candidate instead of generating a duplicate, preserving the parent relationship so it can receive a fair reranker opportunity. Refinement diagnostics distinguish children already represented by broad discovery from children omitted by explicit per-parent/total bounds, so a method can never silently disappear from the refinement report. A test/config hit can likewise generate bounded production candidates only through verified structural edges; neither refinement nor promotion is authority proof.

### Hybrid search pipeline

```text
query
  ├─ exact repository/record search where applicable
  ├─ SQLite FTS (instruction/stop words removed before matching)
  ├─ remote embedding -> Qdrant vector search when the vector index is current
  ├─ reciprocal-rank fusion with per-stage rank/raw-score provenance
  ├─ bounded verified structural candidate expansion from strong test/config hits
  ├─ bounded concrete-symbol refinement from strong production module/file hits
  ├─ focus-aware bounded reranker selection (broad + intent + refined lanes)
  ├─ optional remote reranking of the selected window against the original query
  ├─ scale-independent fused-rank + rerank-rank combination
  ├─ scale-safe, relevance-gated authority prior (implementation/tests/config/balanced)
  └─ deterministic diversity + guarded production representation so duplicate schemas/tests cannot monopolize top-K
```

Qdrant is a derived index, not canonical storage. Awoki queries project vectors only when the last successfully published vector membership matches the current safe source set. The manifest records that semantic snapshot separately as the published membership plus `published_vector_collection`; a local lexical rebuild never rewrites either with an unsynchronized target. A failed redundant refresh cannot invalidate an otherwise matching prior published snapshot: the job reports the failure, while search can continue from the last known-good vectors. Canonical continuity and source files can rebuild the index.

Normal users do not need backend controls. For diagnostics and repeatable A/B/C
tests, `codebase_search` additionally supports real `mode=lexical`, per-query
`use_fts`, `use_qdrant`, `use_reranker`, `structural_promotion`, and
`result_focus=auto|implementation|balanced|tests|config`. Set
`strict_backends=true` when an explicitly requested backend must fail closed
instead of degrading. Unknown explicit modes are rejected rather than silently
becoming conceptual search. Search results expose FTS/Qdrant/fused/rerank/final
ranks and scores where available, plus explicit reranker attempted/applied/backend
telemetry. The canonical live location remains `details.retrieval.reranker`; R9.1.6.12
does not flatten, rename, duplicate, poll, or probe that structure. Captured acceptance
evidence additionally exposes the already-derived bounded selector
`backend_observations.reranker`, which is computed from the same returned telemetry and
causes no extra reranker/backend call. R9.1.3 allocates the finite reranker window through deterministic broad/focus/refinement lanes: an implementation-focused search can select a concrete implementation from deeper in the discovery pool when it has independent admission evidence, a test-focused search can reserve evaluation capacity for tests, and refined/requalified symbols retain bounded evaluation capacity. Lane admission changes only who gets evaluated, never relevance scores or final rank by itself. Code search asks the reranker for a score for every selected window document (subject to the remote backend actually returning one) rather than allowing a configured `top_n=10` transport truncation to decide Awoki's final candidate set. Telemetry distinguishes configured top-N, effective requested top-N, selection-lane counts/reasons, explicit scores actually returned to Awoki, selected candidates without a returned score, and the complete post-rerank pool. `selected_without_returned_score` deliberately makes no claim about whether the remote model internally scored those documents because that is not observable from the current rerank contract. Raw reranker scores are never compared directly with FTS/RRF scores; rank-
based fusion keeps scorer scales independent. For `result_focus=implementation`,
R9.1.3 retains the bounded rank-only composition stage after authority/diversity: a
concrete production implementation can become the implementation anchor only
when its relevance is already close to the semantic leader and it has independent
support (reranker score returned, FTS+Qdrant agreement, or sufficient local query
overlap). A second concrete implementation can be kept inside the top five under
a slightly lower but still explicit relevance floor. Scores are never changed and
weak production code is never inserted to satisfy a quota. The model must never
infer reranker use from score shapes.

For acceptance/debugging, `view=diagnostics` is a real compact response mode.
Awoki serializes backend/reranker/refinement telemetry and `stage_top` before hit
data and omits source previews and repeated parser/score payloads. R10.2 no longer
inlines the complete bounded candidate pool: it stores that metadata-only
`columns+rows` trace behind a short-lived `diagnostic_trace_id`. Pass
`diagnostic_targets=[...]` to `codebase_search` to inline complete records for
important deep candidates, or call `code_diagnostics_trace` by trace id for a
bounded page or a path/symbol target. The primary response also exposes a compact
summary of the finite reranker-selected window. Diagnostic traces are project-scoped, TTL/count bounded in the current Awoki MCP process, contain no source previews, and never become persistent project or backup state.
Reserved-lane eligibility, admission signals/order, and explicit exclusion
reasons remain observability metadata only and do not change ranking behavior.
R9.1.4 closes the composition gap for an existing concrete child that refinement
requalified deep in broad discovery: bounded expansion insertion may evict the
lowest-priority unprotected raw candidate, but it may not evict that requalified
child before the focus/refinement selector evaluates it. The child keeps its
original fused score/rank; preservation grants only a selection opportunity.
Targeted diagnostics also report compact presence/rank at FTS, Qdrant, fused,
post-refinement-discovery, and composed-pool stages so pre-rerank loss is explicit.

R9.1.6 tightens the finite reranker window at the margins without changing RRF,
TEI, or authority weights. Explicit `tests`/`config` focus now treats source role
as scope rather than relevance: a reserved focus slot additionally requires
independent lexical overlap, a strong lexical/semantic backend rank, or bounded
corroborated dual-backend support. Unused focus/refinement capacity is refilled
only by candidates with equivalent independent evidence; low-evidence leftovers
are rejected and telemetry reports `refill_rejected_low_relevance` plus
`unused_budget` instead of blindly forcing the remote window full. The structural
parser also resolves declaration owners through bounded declaration-wrapper
nodes, preserving names such as `MatchingEngine` across grammar shapes instead
of emitting `anonymous_<line>` when a real source-level declaration name exists.
The descent never enters parameter/body/initializer nodes and remains
language-neutral.

R9.1.6.1 fixes the local structural-index lifecycle exposed by large repository
rebuilds. Structural parser semantics now carry an explicit extraction-profile
identity and the engine version is `awoki-structural-code-v8`, so pre-R9.1.6
materializations are deterministically stale and unchanged files are reparsed when
extraction semantics change. Full local refreshes use detached
`code_index_refresh_start` / `code_index_refresh_status` /
`code_index_refresh_cancel` jobs with bounded file/parser progress instead of
running a forced parse inside an interactive MCP request. Existing stale Git
snapshots encountered by MCP search are routed to that background worker, and
`refresh_index=true` starts the same job rather than blocking.

R9.1.5 makes lexical identifier matching language-neutral instead of adding a
Go-specific bearer-token exception. The FTS query keeps the original token but
also derives deterministic separator/case aliases across snake_case, kebab-case,
paths/namespaces, camelCase/PascalCase, and acronym boundaries. A bounded local
identifier bridge compares those literal atoms against indexed path/symbol/
qualified-name/signature metadata and chunk text, then fuses that order with the
native FTS5 order before normal FTS+Qdrant fusion. There is no stemming, semantic
query rewriting, language dictionary, network call, or index/schema migration.
This applies equally to structurally parsed Go/Java/JavaScript/TypeScript and to
text-fallback languages such as Swift; text fallback gains lexical discovery but
not structural-symbol guarantees. Diagnostic target lookup uses the same
language-neutral terminal-member/owner canonicalization, so common Go receiver,
Java/JS/Swift dotted-member, namespace, and Smali spellings can resolve to the
parser-native symbol identity without per-language target aliases.

## Example remote embedding configuration

The bundled example profile assumes:

```text
TEI model: jinaai/jina-embeddings-v2-base-code
OpenAI-compatible API model field: text-embeddings-inference
Vector dimension: 768
```

Use an operator-controlled OpenAI-compatible endpoint. The hostname below is documentation-only:

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

`text-embeddings-inference` is the model label sent to TEI's OpenAI-compatible endpoint. The actual model is fixed by the remote TEI container's `--model-id jinaai/jina-embeddings-v2-base-code` argument. `AWOKI_EMBEDDING_DEPLOYMENT_ID` is operator-declared compatibility metadata for full Qdrant backups; pin it to an immutable model revision when available.

Keep `AWOKI_EMBEDDING_API_KEY` empty only when the remote endpoint has no API authentication. The endpoint receives the safe text being embedded, not merely a hash.

Test the exact API Awoki uses:

```bash
curl -sS \
  "${AWOKI_EMBEDDING_BASE_URL}/embeddings" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "text-embeddings-inference",
    "input": "Where is JWT validation implemented?",
    "encoding_format": "float"
  }' \
  | jq '.data[0].embedding | length'
```

Expected output:

```text
768
```

### Qdrant collection changes

A Qdrant collection has one vector dimension. The old name `awoki_openai_text_embedding_3_large` implied a different embedding model and normally a different dimension, so the Jina/TEI setup uses:

```env
AWOKI_QDRANT_COLLECTION=awoki_jina_embeddings_v2_base_code_768
```

Leave this safety setting at zero:

```env
AWOKI_QDRANT_RECREATE_ON_DIM_MISMATCH=0
```

On a dimension mismatch, Awoki then refuses to delete the collection automatically. Choose a new collection name or deliberately reindex rather than silently destroying an existing vector index.

## Optional TEI reranking

For an optional native TEI reranker at an operator-controlled endpoint:

```env
AWOKI_RERANK_ENABLED=1
AWOKI_RERANK_PROVIDER=tei
AWOKI_RERANK_URL=http://reranker.example.invalid:8000/rerank
AWOKI_RERANK_MODEL=
AWOKI_RERANK_API_KEY=
AWOKI_RERANK_CANDIDATES=30
AWOKI_RERANK_TOP_N=10
AWOKI_RERANK_MAX_DOCUMENT_CHARS=4000
AWOKI_RERANK_FAIL_MODE=fallback
```

For native TEI, the remote container already fixes the model with `--model-id`, so `AWOKI_RERANK_MODEL` normally remains empty. It is not an error and no model field is sent in the native TEI rerank request. `AWOKI_RERANK_API_KEY` must be empty when the reranker has no authentication, or must exactly match the API key configured on the TEI server. `AWOKI_RERANK_API_KEY_ENV` remains useful for a local process that already has the named variable exported; stock Docker/OpenCode Compose cannot dynamically pass an arbitrary variable by name, so use `AWOKI_RERANK_API_KEY` there unless a Compose override explicitly injects the named variable. An explicitly configured indirection is fail-closed across runtimes: an invalid, absent, or empty named credential is reported as a configuration error and Awoki will not send an unauthenticated reranker request. The reranker receives the original query and only the bounded candidate texts selected after hybrid retrieval. In fallback mode, a reranker failure is reported and Awoki preserves the fused FTS/Qdrant order.

Inspect the actual model on the remote Linux host with:

```bash
docker inspect tei-reranker --format '{{json .Config.Cmd}}'
```

After changing `.env`, recreate the baked service before trusting OpenCode's runtime state:

```bash
make opencode-recreate
```

R9.1.6.9 removes the historical stock 5-second code-search reranker override. New installs leave `AWOKI_CODE_RERANK_TIMEOUT_SECONDS` empty so code search inherits `AWOKI_RERANK_TIMEOUT_SECONDS` (20 seconds by default). Existing `.env` files are never rewritten automatically; if yours still contains `AWOKI_CODE_RERANK_TIMEOUT_SECONDS=5` from an older template and that was not intentional, clear or remove that value before recreation. Explicit shorter/longer code-search overrides remain supported.

Then start a new OpenCode process and run `/retrieval-status`.

## `/codebase`

Example:

```text
/codebase How is JWT issuer validation enforced?
```

The first call explicitly enables safe indexing for the project repository. Awoki
parses eligible source into functions, classes, methods, interfaces, modules, and
references and stores definitions/branch membership in a dedicated SQLite code
index. Interactive `codebase_search` does **not** synchronously rebuild Qdrant or
embed the repository: a clean Git commit reuses the existing structural snapshot,
and semantic vectors are queried only when an already-materialized membership is
current. If vector retrieval is stale or a bounded query dependency fails, local
exact/FTS/structural discovery still runs and reports the semantic degradation.
Use `repository_prepare_start` for normal end-to-end readiness and `code_vector_refresh_start` only when direct vector regeneration is intentionally requested. Both return control immediately. The parent readiness job can own vector regeneration internally and then verify exact membership/backend health without requiring another model turn. `code_vector_refresh_status` still provides real chunk/vector/batch progress for direct diagnostics. Awoki preflights Qdrant collection materialization before embedding, persists completed batches incrementally, retries transient request failures boundedly, and can reduce repeatedly timing-out request batches. Unchanged chunks and already-persisted successful sub-batches are reusable across later attempts. Failure responses preserve reused/persisted vectors, completed batches, failing phase/batch, and remaining work; incomplete membership does not satisfy `FULL_READY`.

Natural language is the default interface. A deterministic router selects lexical,
exact, definition, callers, callees, path, similar-code, or conceptual search and reports
its choice. Ask `/codebase` for “locations only”, bounded context, or complete
symbol bodies when result depth matters. Bounded context preserves the requested
top-K result metadata by sharing preview budget across hits instead of silently
dropping later ranks. Repository behavior is evidence-backed by
default: search results discover candidates, exact definitions resolve entry
points, the internal `code_flow_graph` tool builds a bounded relevant reachable
subgraph, and `code_source_window` returns hash-checked current source with hard
per-line/total bounds plus an `evidence_id` for later stale-evidence verification.
The ID is not a cryptographic signature. Flow explanations inspect branch predicates, local
assignments/aliases, arguments, returns, and outcomes rather than treating the
call graph alone as full data/control flow. Use `/definition`, `/callers`,
`/callees`, `/code-path`, and `/code-across` when deterministic control is useful.
Cross-project scope is never implicit.

`/code-validate-claim` accepts either an atomic claim or a broader request such as
validating a decision tree or file-processing flow. Broad requests are first
discovered and decomposed into exact obligations; the strict `code_validate_claim`
MCP primitive then re-resolves definitions against fresh hashed source, applies
supported AST and lexical-scope proof obligations, and validates candidate graph
paths edge by edge without embeddings or reranking. Generic graph resolution alone
is never treated as proof;
unsupported or ambiguous obligations return `INCONCLUSIVE` rather than a guess.
The strict primitive is used selectively underneath broader investigation; users
do not need to phrase ordinary “explain/trace/understand” questions as atomic
claims.

Awoki separates source-snapshot assurance from human identity. A deep
index/verify may establish `VERIFIED_SNAPSHOT`; dirty or unusual Git views become
`WORKING_TREE_BOUND`; intentional non-Git trees remain `FILESYSTEM_BOUND` and are
still searchable. Author/committer fields are recorded as Git metadata claims,
not verified people. Replace refs, sparse checkout, submodules, configured
content filters, assume-unchanged/manual skip-worktree flags, weakened Git stat
trust, and similar view state are disclosed/lower assurance rather than censoring
code. Passive repository reads disable fsmonitor and lazy promisor-object fetching,
remove repository-rebinding/transient Git environment overrides, and neutralize
configured content-filter execution. The hot-path view identity uses cheap index
stat identity while explicit deep verification streams an index-content hash and
inspects status-suppressing flags, preferring a conservative/lower-assurance result
over running repository/local helper commands or contacting a remote.

When a conclusion depends on a supported Go primitive, use
`code_semantics_check`; this is specifically intended to prevent reasoning errors
around `time.Duration`, failed type assertions, and path/string/URL helpers. The
result names the helper Go toolchain and compares it with an attached project's
plain-text `go.mod` declaration, so a version-sensitive stdlib observation is not
silently promoted across a toolchain mismatch.

Choose exact-search tooling by intent. Use Awoki indexed/structural discovery for
conceptual questions, OpenCode Grep for ordinary exact string/symbol lookup, and
`code_exact_search` when structured ripgrep features materially improve complex/exhaustive
exact enumeration. When structured exact-search output cannot safely establish the
coverage required for a claim, use `code_text_search`: the Awoki-owned
ripgrep interface exhaustively scans permitted repository source, materializes the
result once, exposes giant-line-safe byte offsets plus bounded previews, and
uses snapshot-bound cursors for both scan resumption and result pagination.
Continue until `scan_complete=true`, then until `search_complete=true`, with
`repository_universe_complete=true`; do not add `head` or another truncation layer. The CLI
helper is only the MCP-unavailable equivalent.
Fallback output is discovery-only; policy-excluded or still-unindexed candidates
remain an explicit inconclusive evidence boundary.

Repository code analysis is coverage-first. Programming source and tracked textual repository formats do not disappear because they mention authentication, tokens, passwords, secrets, JWT/OAuth, or live under security-named packages. Unsupported textual non-prose source/config formats remain available to exhaustive lexical search and participate in primary `codebase_search` through deterministic text-fallback chunks even when no structural parser exists. Obvious textual secret/config files and tracked generated/vendor text are local-lexical-only: they are kept out of structural/vector indexing but can still contribute exhaustive local matches (with opaque previews for explicit sensitive paths). Explicit no-RAG source and unsafe symlinks remain intentional exclusions and therefore prevent a whole-repository completeness claim. High-confidence credential values are redacted best-effort before derived source text is stored, embedded, or returned; Awoki prioritizes analysis coverage over a zero-leak guarantee. See
`docs/CODE_SEARCH.md` and `docs/CODE_SEARCH_EVALUATION.md`.

## Degraded retrieval behavior

If the remote embedding endpoint or Qdrant is unavailable:

- continuity capture and generated views continue;
- exact SQLite/JSONL retrieval can still work;
- semantic indexing/search reports the degraded state;
- Awoki does not fabricate semantic results.

Use passive `retrieval_status` and `code_index_status` to inspect effective configuration and materialized state without network calls or repository-wide rescans. Their Qdrant health is last-known/recorded state, not an implicit live check. Use explicit `retrieval_probe` for bounded Qdrant/embedding/reranker connectivity checks and `code_index_verify` for byte-level source freshness plus optional live code-Qdrant verification. Interactive code retrieval defaults to a 5-second/no-retry query-embedding budget and a 2-second Qdrant client budget. Code reranking now inherits the shared `AWOKI_RERANK_TIMEOUT_SECONDS` (20 seconds by default) unless `AWOKI_CODE_RERANK_TIMEOUT_SECONDS` is explicitly set; this avoids the historical 5-second code-search cap causing false TEI timeouts on 30-document windows. Bulk indexing uses separate embedding timeout/retry settings.

## Backup and restore

Awoki includes verified portable and full runtime-data migration commands:

```bash
make backup-portable
make backup-full
make backup-inspect BACKUP=/path/to/awoki-*.tar.gz
make backup-verify BACKUP=/path/to/awoki-*.tar.gz
make restore BACKUP=/path/to/awoki-*.tar.gz
```

Backups default to `../awoki-backups`, outside the repository. Every archive has a `.sha256` sidecar, is internally re-verified after creation, and has mode `0600`. The checksum detects accidental or unauthorised modification only when the sidecar itself is trusted; it is not a signature or proof of origin. Backup and restore refuse active Awoki Compose services by default; use `BACKUP_STOP_CONTAINERS=1` or `RESTORE_STOP_CONTAINERS=1` to stop them explicitly. Full backup and every restore require quiescence; only portable capture has an explicit live acknowledgement. Standard launchers honour the operation lock. Containers remain stopped afterward.

A **portable** backup contains canonical project/global data and self-contained repositories but excludes SQLite indexes, project `index/` directories, Qdrant storage, Lavish runtime state, `.env`, SSH keys, and OpenCode state. Portable restore clears stale derived indexes and automatically rebuilds lexical indexes for every restored project and every restored global root. Repository code indexing remains opt-in through `/codebase`. Rebuild semantic vectors with `make index-vector`, or restore with `RESTORE_REINDEX=vector`. When multiple global roots are restored, vector-mode restore indexes the preferred repo-local Docker/SSH global root and reports that any alternate host-local global root needs an explicit later indexing run.

A **full** backup additionally includes local/project indexes and `data/qdrant/`. All Awoki Compose services must be stopped because the captured indexes and Qdrant storage are mutable. Full restore compares vector size, collection, provider, request-model label, normalisation setting, operator-declared actual deployment identity, Qdrant image reference, and image identity when available. Non-empty Qdrant state without `AWOKI_EMBEDDING_DEPLOYMENT_ID` on both installations is treated as unproven compatibility. A mutable `:latest` image without a provably matching digest/ID is also blocked unless force is explicit. Prefer portable restore plus reindexing when versions or retrieval configuration differ.

Known installation credentials remain excluded unless explicitly requested:

```bash
make backup-full BACKUP_INCLUDE_OPENCODE_STATE=1
make backup-full BACKUP_INCLUDE_SECRETS=1
```

OpenCode state can contain authentication and conversation data; `BACKUP_INCLUDE_SECRETS=1` includes `.env` and `.ssh-container/`. Default archives can still contain sensitive repositories, evidence, continuity, and explicit secret/no-RAG records, so treat every backup as private. Linked Git metadata/object stores outside `workspace/`, special files, and unsafe symlinks are rejected rather than silently omitted. Docker named volumes for Neovim and SSH server host keys are recreated rather than archived.

Restore refuses path traversal, escaping links, special files, malformed or mode-inconsistent payloads, missing/mismatched checksums, every live Awoki service, overlapping/symlink-resolved or overly broad destinations, and overwriting existing runtime data. Use `RESTORE_FORCE=1` only after inspecting the destination and archive. See `docs/BACKUP_RESTORE.md` for the complete inclusion boundary, compatibility checks, and recovery procedure.

## UID/GID on Docker Desktop

The OpenCode image creates its non-root `op` user from build arguments derived from:

```env
AWOKI_HOST_UID=1000
AWOKI_HOST_GID=1000
```

These values are numeric Linux ownership IDs; they do not import host users or groups. On native Linux, matching host IDs can prevent bind-mounted files from becoming owned by an unrelated numeric user. On macOS Docker Desktop, the default `1000:1000` normally works and matching macOS values such as `501:20` is usually unnecessary.

If the two variables are removed from `.env`, Compose falls back to `1000:1000`. Changing them affects image build-time user creation and requires rebuilding the image. Existing named-volume files retain their previous numeric ownership and may need a deliberate ownership migration. Do not use UID or GID `0` for the normal Awoki user.

## Sensitive memory

Normal captures preserve security-analysis content and redact high-confidence credential values best-effort without automatically hiding the record from retrieval. When you explicitly ask Awoki to save sensitive plaintext, use explicit sensitive capture. The value is preserved append-only, marked secret/no-RAG, omitted from generated situation/handoff, and returned only through explicit sensitive retrieval.

This is generic memory, not an encrypted credential vault. An external Keychain, Bitwarden, or other credential skill can be added later under `.opencode/skills/<name>/SKILL.md`.

## Lavish

`/lavish` is ad hoc only. It stages an HTML artifact under the ignored `workspace/.lavish/` area, starts the configured pinned Lavish version with browser opening disabled inside the container, and exposes only `127.0.0.1:${AWOKI_LAVISH_PORT:-4387}` on macOS. Its shell launch uses the non-retrieval `lavish` runtime profile so custom port/version/state settings survive SSH without placing embedding/reranker API keys in `npx`'s environment. Run `./open-lavish.sh` on the host to open the browser. No ambient Lavish hook or third-party share is enabled.

## Awoki MCP troubleshooting

When OpenCode reports `MCP server awoki ... Process exited with code 1`, inspect the durable stderr log and run the compatibility preflight inside the running container:

```bash
docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh \
  /awoki/.harness/bin/mcp-preflight

docker compose -f docker-compose.opencode.yml exec -T awoki-opencode-ssh \
  tail -n 100 /awoki/.harness/state/mcp-local.stderr.log
```

A fresh image must report an MCP 1.x version. Rebuild with `--no-cache` after changing `requirements.txt`; the Dockerfiles and SSH entrypoint fail early when the installed SDK or Awoki server imports are incompatible. MCP stderr is mirrored to both OpenCode and `.harness/state/mcp-local.stderr.log`.

## Strict local-model chat templates

Some Qwen-family GGUF chat templates served by llama.cpp reject multiple or non-leading system messages with:

```text
Unable to generate parser for this template
Jinja Exception: System message must be at the beginning.
```

Awoki therefore delivers permanent reliability rules through `AGENTS.md` and the `instructions` entries in `opencode.jsonc`; its continuity plugin does **not** append a system message through `experimental.chat.system.transform`. The plugin adds bounded continuity/reliability text plus a small execution-invariant section to compaction context. This avoids introducing the extra system prompt known to break strict Qwen/llama.cpp templates while still preserving the MCP/acceptance execution boundary when OpenCode compacts automatically.

A different local-model failure class is a terminal assistant turn that contains a
reasoning part but never emits normal text or a tool part. A related case is a completed
tool execution followed by no normal assistant continuation. Awoki observes OpenCode's
`message.updated`, `message.part.updated`, tool-execution, and `session.idle` events and
records only structural metadata as `reasoning_only_terminal_turn` or
`tool_execution_without_followup`. Use
`session_runtime_status` to correlate it with provider/model/agent-mode and the exact
resolved OpenCode CLI/plugin/SDK image tuple. The reasoning text itself is never copied
into Awoki state, and this detector does not auto-submit `continue`.

After updating this behaviour, rebuild and recreate the OpenCode container and start a new session. If the error still occurs with Awoki disabled, isolate the provider/template independently:

```bash
mkdir -p /tmp/opencode-empty
cd /tmp
OPENCODE_CONFIG_DIR=/tmp/opencode-empty \
  opencode --pure --print-logs --log-level DEBUG
```

A failure in that empty/pure configuration is outside Awoki and indicates the model server's chat template or another provider-side tool-parser incompatibility. Do not patch Awoki memory data to work around it; use a compatible/patched chat template or model-server configuration.

## Burp

Burp is an optional adapter, not project boilerplate. Creating/opening a normal project does not create `artifacts/burp/`, and generic memory/RAG does not inject saved Burp inventories into unrelated projects. Project Burp storage appears lazily after explicit Burp preservation/write activity.

Burp MCP on macOS remains at `127.0.0.1:9876`. The OpenCode container reaches it through `http://host.docker.internal:9876`; no host networking is used.

Use `/burp` for natural-language live inspection, searching, host summaries, saved-run lookup, and preservation. Use `/burp-repeater`, `/burp-intruder`, or `/burp-send` only when the corresponding side effect is explicit. Staging a request in Intruder does not authorize launching an attack. Awoki records compact safe observations and continuity rather than replacing direct Burp MCP.

When Burp MCP is not running, disable `mcp.burp.enabled` in both relevant OpenCode configurations before rebuilding or diagnose OpenCode with `opencode --pure --print-logs --log-level DEBUG`.

## Filesystem and Git

Awoki tool source is tracked. The entire runtime `workspace/` tree and mutable harness memory/state are ignored. Release archives are built from tracked Git content only.

The normal container does not mount the host repository over `/awoki`. See `docs/ARCHITECTURE.md`, `docs/FILESYSTEM.md`, `docs/OPENCODE_SSH.md`, and `docs/BACKUP_RESTORE.md` for the complete storage and migration boundary.

## Validation

```bash
make validate
```

`make validate` is the dependency-tolerant host/hermetic gate: repository contracts, JSON/JSONC, Python, unit tests, shell syntax, OpenCode plugin checks, and any locally available parser/Docker checks. It does **not** pretend that a missing host `rg`, parser package, Qdrant, or Docker runtime was exercised.

For the runtime-strength gate use:

```bash
make validate-runtime
```

That target first runs `make validate`, then requires a real ripgrep executable, the prebuilt Go semantics helper (or the fixed-source local Go fallback), and the bundled Tree-sitter runtime, then executes the runtime code-search suites (including live Qdrant behavior).

See `docs/CODE_SEARCH_IMPLEMENTATION_PLAN.md` for the final native architecture.
