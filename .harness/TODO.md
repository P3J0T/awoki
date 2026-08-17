# Awoki Harness TODO

This backlog tracks harness/runtime work, not user project tasks.

## Completed in R4

- [x] MCP-native multi-repository management: `project_repo_add`, `project_repo_list`, `project_repo_remove`, `project_repo_default`.
- [x] Natural-language repo registration can infer `repo/<repo-id>` from requests such as “add repo oathkeeper”.
- [x] Repository registration reports exact Git-root state when Git is present and refuses nested Git-root mismatches.
- [x] `project_open` passively inspects structural/vector freshness for every enabled repository and returns `repository_index_advice`.
- [x] Newly registered repositories immediately return the same semantic-index advice.
- [x] Missing/stale semantic vectors produce an explicit opt-in background vector-refresh recommendation; opening a project never triggers remote embedding implicitly.

## Completed in R5 audit

- [x] Synchronize README/AGENTS/HARNESS and project/filesystem/code-search/reliability docs with multi-repo behavior and passive semantic-index advice.
- [x] Synchronize `.harness/manifest.json` with the complete exposed MCP tool surface and add validation that fails on either missing or stale manifest tools.
- [x] Wire `/reliability-check`, `/ship-check`, and the reliability skill to the structured claim/verifier gate; ship workflow now explicitly starts with `mode="ship"`.
- [x] Harden MCP repo registration so absolute/escaping/container-root paths are rejected before any repository probing or registration.

## Completed in R6 runtime hardening

- [x] Replace blocking interactive code-vector materialization with detached `code_vector_refresh_start` / `code_vector_refresh_status` / `code_vector_refresh_cancel` jobs so MCP remains responsive beyond the OpenCode request deadline.
- [x] Persist newly embedded code vectors to Qdrant incrementally by embedding batch so interrupted first-time materialization can reuse completed content-addressed points.
- [x] Teach project-open/repo-add advice and natural-language `/project` routing to use the background code-vector job instead of synchronous `project_refresh(include_qdrant=true)`.
- [x] Document that first-time vector materialization can be CPU-intensive at the configured embedding backend while later unchanged chunks are reused.

## Completed in R7 vector-refresh observability

- [x] Expose real detached vector-refresh progress: phase, chunks ready/total, target/reused/persisted vectors, batches completed/total, percentage, collection, and elapsed time without storing source text in job state.
- [x] Preflight/materialize the Qdrant code collection before the first expensive embedding batch so storage/collection failures fail fast without burning embedding CPU.
- [x] Create and validate `data/qdrant/collections/` during runtime initialization for bind-mounted Qdrant storage.
- [x] Add OpenCode/tool guidance that long-running vector jobs should return the job id and control immediately rather than being synchronously awaited.


## Completed in R8 runtime/tool boundary audit

- [x] Add a guarded `awoki-runtime-env` diagnostic wrapper for the SSH runtime handoff with clean child environments, symlink/trust checks, URL redaction, and least-privilege profiles (`base`, `qdrant`, `retrieval`, `burp`, `lavish`, internal `mcp`, `all`).
- [x] Make runtime diagnostics usable from either macOS/host Make targets or from inside the OpenCode SSH container without manually sourcing `/run/awoki/runtime.env`.
- [x] Add fixed-synthetic `embedding-benchmark` and `reranker-benchmark` commands that use the exact effective MCP retrieval configuration without sending repository content.
- [x] Carry custom Lavish runtime settings across SSH without placing retrieval secrets in `npx`'s inherited environment; keep live Burp on direct `mcp.burp` while giving Burp archive helpers a non-retrieval runtime profile.
- [x] Relaunch container Awoki MCP through a clean internal `mcp` profile so stale SSH/OpenCode `PATH`, `PYTHON*`, `LD_*`, proxy, and unrelated variables are not ambiently inherited by the secret-bearing MCP process.
- [x] Make reranker API-key indirection fail closed across SSH and non-SSH runtimes when an explicitly named credential variable is invalid, absent, or empty; never silently issue an unauthenticated reranker request in that case.
- [x] Validate the runtime boundary across host Make targets, SSH diagnostics, Qdrant-only probes, synthetic embedding/reranker probes, direct Burp MCP, Burp archive helpers, Lavish, and MCP startup; document that profile filtering is environment minimization rather than same-user isolation.
- [x] Strip retrieval/provider credentials plus ambient loader/interpreter/SSH-agent/Git-SSH overrides from repository-facing Git and ripgrep subprocesses; keep deterministic Go semantics on its pre-existing fixed clean environment.

## Completed in R8.1 macOS/hermetic validation hotfix

- [x] Keep `awoki-runtime-env` and the SSH runtime handoff compatible with macOS system Bash 3.2 by avoiding Bash 4.2-only `[[ -v ... ]]` tests; retain indirect-variable checks with portable `declare -p`.
- [x] Keep unresolved-reranker credential tests hermetic when host Python does not install `httpx`; inject the optional runtime module at the import boundary and prove no network call occurs.
- [x] Re-run the full host/hermetic validation and all code-search acceptance suites after the portability fix.

## Completed in R9 retrieval-quality hardening

- [x] Add real lexical-only retrieval plus per-query FTS/Qdrant/reranker controls and strict fail-closed backend experiments; unsupported explicit modes no longer silently become conceptual search.
- [x] Preserve machine-readable FTS/Qdrant/fused/rerank/final stage provenance and explicit reranker requested/attempted/applied/backend telemetry so models never infer execution from score shapes.
- [x] Keep tests/config/schema/docs in the discovery universe while adding result-focus-aware authority ranking and deterministic diversity for implementation/security queries; explicit test/config queries retain those roles.
- [x] Add bounded verified structural candidate expansion from strong non-production hits, with reserved reranker capacity and safeguards so graph connectivity never becomes behavioral authority by itself.
- [x] Preserve requested top-K metadata when preview budgets are exhausted instead of silently dropping later ranked candidates.
- [x] Separate lexical publication from the last successfully published vector membership/collection so harmless local rebuilds preserve current vectors while source/semantic/collection identity changes mark them stale.
- [x] Enforce vector-job polling cadence in the protocol with `recommended_poll_after_seconds`, `poll_too_soon`, and `retry_after_seconds` rather than relying only on prompt guidance.
- [x] Preserve truthful partial vector-refresh progress/failing batch telemetry and skip unchanged Qdrant payload mutations during full-reuse refreshes.
- [x] Add a self-development boundary: agents must verify a writable top-level Awoki Git checkout before product modification and must not privilege-escalate/retake ownership of the hardened `/awoki` runtime appliance.
- [x] Add retrieval-quality regressions for authority intent, structural-promotion safety, backend isolation, collection identity, vector-state preservation, and result-budget correctness.

## Completed in R9.1 concrete-symbol refinement

- [x] Refine strong implementation-focused production module/file candidates into bounded concrete child functions/methods from existing structural parent/child metadata; parent relevance grants evaluation capacity only, never inherited authority.
- [x] Reserve refined/promoted candidates inside the remote reranker's actual candidate window so structural discovery is genuinely evaluated rather than appended outside the scored set.
- [x] Separate coarse production modules/contracts from concrete production implementations in authority classification so interface-only production files do not receive implementation boosts.
- [x] Combine fused retrieval rank and reranker rank with scale-independent reciprocal-rank fusion instead of replacing retrieval scores with incomparable remote reranker scores.
- [x] Make reranker accounting explicit: selected/request documents, scores returned, scored/unscored candidates, and post-rerank pool size are distinct telemetry fields.
- [x] Keep the semantic reranker authority-neutral; production/test/config authority is applied only after relevance reranking.
- [x] Correct lexical-mode vector skip telemetry so disabled/not-requested vectors are not described as unavailable.
- [x] Add live-style synthetic regressions proving a coarse module can refine to a relevant concrete method while an unscored child cannot inherit its parent's rank.

## Completed in R9.1.2 result composition and telemetry precision

- [x] Make refinement completeness observable: report concrete children available, already represented by broad discovery, generated, and omitted by per-parent/total limits instead of silently calling an already-present or bounded child a refinement gap.
- [x] Clarify reranker semantics: distinguish configured top-N and explicit scores returned to Awoki from selected candidates without a returned score; do not claim the remote model failed to score documents internally when its contract does not expose that fact.
- [x] Add per-candidate `rerank_selected` and `rerank_score_returned` flags so agent reports never infer candidate scoring from null ranks.
- [x] Add bounded rank-only implementation composition after authority/diversity: independently strong concrete implementations can anchor the result set or stay inside the top five only when they already satisfy explicit relevance floors; weak production code never fills a quota.
- [x] Keep explicit test/config focus unchanged and preserve all underlying retrieval/rerank/authority scores while reporting composition rank movements separately.

## Completed in R9.1.3 focus-aware reranker coverage

- [x] Requalify concrete symbols that were already present in broad discovery when a strong coarse parent refines to them; deduplication no longer removes their bounded reranker opportunity.
- [x] Allocate the finite reranker window deterministically across broad discovery, result-focus candidates, and refined/promoted candidates, then refill unused capacity from broad order.
- [x] For implementation focus, admit deeper concrete implementations only when an independent selection signal exists (strong refined parent, dual FTS+Qdrant support, or bounded local query overlap); weak production code receives no role-only reranker slot.
- [x] For explicit test/config focus, reserve reranker capacity for matching source roles across the complete candidate pool rather than only the global broad cutoff.
- [x] Request explicit reranker scores for the entire selected code-search window when the backend contract supports them; preserve configured top-N separately and expose any returned-score shortfall honestly.
- [x] Expose reranker selection lane/reason plus pool/budget/general/focus/refined/refill telemetry so candidate-budget decisions are reconstructable.

## Completed in R9.1.4 requalified-candidate survival

- [x] Preserve an already-discovered concrete child that refinement requalified when bounded expansion composition would otherwise evict it before reranker selection; preserve its original score/rank and evict only a lowest-priority unprotected raw candidate.
- [x] Add an end-to-end regression covering discovery/refinement -> bounded composition -> focus-aware reranker selection for a deep existing concrete child.
- [x] Expose compact diagnostic-target presence/rank at FTS, Qdrant, fused, post-refinement-discovery, and composed-pool stages so a pre-rerank loss is attributable without scraping client tool-output files.
- [x] Keep diagnostic final hits capped at top 10 while full candidate inspection remains available through the R10.2 process-memory trace handle.

## Completed in R9.1.5 universal lexical normalization

- [x] Normalize common code identifier spelling boundaries language-neutrally across separator styles, camel/Pascal case, and acronym boundaries while preserving the original query token.
- [x] Add a bounded local identifier/text bridge so middle-of-camelCase identifiers and text-fallback languages can receive lexical support without FTS schema migration, stemming, network calls, or source execution.
- [x] Fuse native FTS5 and normalization-bridge order deterministically before the existing FTS+Qdrant rank fusion; keep the bridge as discovery evidence only.
- [x] Make diagnostic target matching owner-scoped and parser-format-neutral across receiver/dotted/namespace/Smali-style member spellings rather than special-casing Go.
- [x] Add cross-convention regressions covering Go, Java, JavaScript, Swift text fallback, Smali-style target spelling, and false-owner diagnostic matches.

## Completed in R9.1.6.18 J2 stabilization

- [x] Promote full-strength ripgrep exact search to first-class `code_exact_search` MCP instead of Bash command construction: repository-scoped, typed patterns/modes/globs/context/hidden/ignored policy, credential-stripped subprocess environment, source-aware redaction, bounded continuation metadata, and no raw CLI passthrough.
- [x] Slim normal `project_open` output to repo/readiness + current session work + bounded prior-material pointers/continuation guidance. Keep dense SITUATION/HANDOFF/reflection projections behind explicit `project_resume` / `project_handoff` / `project_search` calls.

## Next

- [ ] Reproduce and fix tmux/OpenCode scrolling over SSH on macOS: with tmux mouse mode and a Mac touchpad, two-finger scrolling can move terminal history instead of the OpenCode chat viewport (or mouse-mode interception is wrong). Test tmux mouse mode, alternate-screen behavior, terminal-emulator interaction, and OpenCode TUI scrolling with a repeatable Mac touchpad case.
- [ ] Move retrieval API credentials out of the same-user-readable stdio MCP runtime snapshot. The current profile wrapper reduces accidental environment inheritance but is not a sandbox because `op` must be able to read `/run/awoki/runtime.env`; evaluate a dedicated MCP sidecar/broker or equivalent design before claiming hostile same-user secret isolation.
- [ ] Tune separate bulk-materialization versus interactive-query timeout/batch defaults from measured `embedding-benchmark` latency; keep query search fail-fast while making first-time indexing resilient to slow local/remote embedding servers.
- [ ] If deployments require corporate proxies or private TLS roots, add explicit allowlisted proxy/CA configuration with redacted diagnostics; do not re-enable ambient `HTTP_PROXY`/`SSL_CERT_*` inheritance into MCP just to make one deployment work.
- [ ] Cache deterministic semantics verifier receipts by language/toolchain/operation/canonical inputs so repeated immutable probes are reused rather than rerun.
- [ ] Add deterministic derived-artifact ingestion recipes for DEX/APK and compiler/decompiler/disassembler exports (for example baksmali, JADX, Ghidra, IDA, Rizin/objdump), recording parent content identity, tool/version, arguments, and output identity. Do not execute those toolchains implicitly.
- [ ] Add native-binary program-entity ingestion (function/basic-block/instruction/address/xref/import/export) with byte/RVA-backed evidence locators and explicit primary-vs-derived evidence authority.
- [ ] Add pseudocode/decompiler authority classes and lineage-aware composition so derived interpretations can aid discovery without outranking contradictory primary instructions/source evidence.
- [ ] Generalize the language semantics provider registry beyond the current Go-specific implementation.

## Retrieval observability

- [x] Replace inline full-pool diagnostics with short-lived process-memory/project-scoped diagnostic trace handles, explicit target records, and bounded paged/targeted MCP retrieval.
- [ ] Keep compiler/decompiler/disassembler execution and generated native-binary derivation pipelines deferred; textual evidence-source support must not silently execute toolchains.
