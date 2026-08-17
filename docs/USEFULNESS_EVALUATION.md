# Awoki Stabilization and Usefulness Evaluation

## Purpose

R9.1.6.16 marked a deliberate development-phase change. R9.1.6.17 applied J1 findings; R9.1.6.18 applied only the two J2 changes that were explicitly selected: first-class structured exact search and a slimmer `project_open` projection. J3 then exercised both in a realistic authenticator-ordering review and classified both KEEP: `code_exact_search` replaced a real multi-file raw-rg enumeration cleanly, and slim `project_open` retained the useful prior-review pointers while eliminating the duplicated continuity dump. R9.1.6.19 added operator-onboarding clarity only. v0.1.0 is the first public semantic-versioned release of this stabilization baseline and does not add a new analysis mechanism.

The previous releases established many correctness mechanisms. Green regression tests prove that those mechanisms usually behave according to their contracts; they do **not** prove that every mechanism improves real security/code-review work.

The next phase therefore freezes discretionary feature expansion and evaluates Awoki through realistic investigations. The output of this phase should include deletions, merges, simplifications, and UX changes—not only additions.

## J3 evidence recorded

- `code_exact_search`: **KEEP** — one production-only, multi-pattern, context-bearing call returned eight complete matches with `has_more=false`; no raw-rg command construction or retry was needed.
- slim `project_open`: **KEEP** — prior J1/J2 report pointers and repository readiness were useful; dense SITUATION/HANDOFF/reflection projections were not needed and `project_resume` was never called.
- bounded reflection: continued to earn its place by narrowing the disabled-handler claim and extending the ordering analysis to any three-dot token and `oauth2_introspection`.
- automatic compaction: still not exercised in J3, so TODO-backed goal continuity across a naturally triggered compaction remains an open Journey question.

## Primary question

> Can Awoki's machinery disappear underneath a good security/code-review conversation while still making the final analysis more accurate, auditable, and resilient to compaction?

## Evaluation principles

1. Use natural user prompts, not acceptance-schema prompts.
2. Let the agent investigate freely within existing security/scope boundaries.
3. Measure whether durable mechanisms materially help.
4. Preserve honest `INCOMPLETE`/contradicted outcomes.
5. Do not tune the scenario merely to make a mechanism look useful.
6. Prefer real repositories and realistic tasks.
7. Include intentional traps where naive retrieval/reasoning commonly fails.
8. Compare against a simpler baseline when practical.
9. Treat unnecessary persisted state/tool calls as costs.
10. A feature can pass its unit tests and still be a candidate for removal.

## Observed journey evidence

### J1 — bearer-token authentication review (completed)

The first realistic review produced useful architecture/security conclusions and
also exposed concrete product friction. Treat these as stabilization evidence, not
as reasons to add unrelated machinery:

- **KEEP:** revision/scope assurance was effectively free and kept source claims
  bound to the reviewed snapshot.
- **KEEP:** indexed/semantic discovery found the relevant authentication/request
  handling implementation quickly, while exact source remained the authority.
- **KEEP / measure further:** the bounded observation/inference/unknown reflection
  checkpoint improved claim hygiene; later journeys must measure whether it changes
  or narrows conclusions rather than merely restating them.
- **SIMPLIFY:** exact lexical work should not be artificially prohibited. OpenCode
  Grep is appropriate for ordinary exact lookup; Awoki `code_exact_search` is the
  first-class structured-ripgrep path for complex/exhaustive enumeration; `code_text_search` remains the deterministic
  exhaustive-coverage path when needed.
- **SIMPLIFY:** project-memory/general-RAG freshness must be named separately from
  structural code-index freshness.
- **SIMPLIFY:** compaction should inject references actually used by the current
  session, not recent references from unrelated older acceptance/review sessions.
- **SIMPLIFY:** source-window truncation must tell the agent exactly why the range
  was incomplete and how to continue.
- **REMOVE BOILERPLATE:** an Awoki-generated project-local `AGENTS.md` containing
  no project-specific rule caused repeated per-read reminder overhead and should
  not be created.
- **GOAL CONTINUITY:** use the existing bounded native TODO mirror as the active
  multi-step deliverable/constraint working set; do not add a second session-intent
  ledger unless later journeys prove that mechanism insufficient.

## Recommended first 12 journeys

### J1 — Dynamic reachability trap

Prompt: review an authentication implementation and decide whether a handler is reachable.

Repository characteristic: direct static callers are absent because registration/factory/config drives execution.

What good looks like: Awoki treats “no direct caller” as negative evidence, creates a reachability hypothesis/gap, and follows registration/config rather than declaring unreachable.

### J2 — Test/implementation disagreement

Prompt: determine current behavior for an edge case.

Repository characteristic: an old test implies X while current implementation does Y, or test coverage is stale.

What good looks like: production implementation is current behavioral evidence; the stale test becomes a contradiction/regression clue, not runtime proof.

### J3 — Interface/default implementation trap

Prompt: determine whether a security check always executes.

Repository characteristic: interface/default implementation checks the property but one concrete implementation bypasses/overrides it.

What good looks like: Awoki resolves concrete implementations and refuses a universal claim until all relevant dispatch paths are addressed.

### J4 — Negative lexical evidence

Prompt: determine whether secret/token validation exists.

Repository characteristic: obvious vocabulary is absent; behavior is encoded through generic helper/factory names.

What good looks like: structural/semantic discovery broadens beyond literal keywords, then exact source is inspected before conclusions.

### J5 — Configuration-dependent behavior

Prompt: decide whether a potentially dangerous implementation is active.

Repository characteristic: source supports several handlers/modes and deployment config is incomplete/unavailable.

What good looks like: implementation possibility is separated from deployed reachability; result can remain `INCOMPLETE`.

### J6 — Cross-layer authentication → authorization flow

Prompt: find every rejection/identity-construction point before authorization and follow principal state into the authorization layer.

What good looks like: bounded flow decomposition, relevant source/call graph, no giant graph dump, explicit unresolved dynamic boundaries.

### J7 — Security finding promotion

Prompt: investigate a plausible bypass and decide whether it deserves a finding.

What good looks like: hypothesis can remain provisional; promotion triggers bounded reflection/verification; support/refutation/gaps are visible.

### J8 — Contradictory evidence

Prompt: review a behavior where two code paths/tests/configs suggest incompatible outcomes.

What good looks like: contradiction is recorded prominently instead of averaged away; unrelated verified facts can remain verified.

### J9 — Compaction-heavy investigation

Prompt: run a long multi-branch review until one or more automatic compactions occur, then continue naturally.

What good looks like: important references, hypotheses, TODOs, project scope, and verification state recover without the user managing IDs or reconstructing the plan.

### J10 — Human reference usability

During a long review ask: “go back to the bearer-token evidence,” “what did we conclude about handler reachability?”, “show what contradicts that finding.”

What good looks like: labels/why_saved help; ambiguous phrases do not silently choose an ID; stable IDs remain mostly invisible unless needed.

### J11 — Backend degradation

Make semantic/reranker backend unavailable or slow while local indexes remain usable.

What good looks like: local review continues when policy allows; backend degradation is explicit; no invented reranker scores; product correctness is not conflated with provider availability.

### J12 — Burp + source correlation

Use read-only Burp observations together with source analysis to explain a live authentication/session behavior.

What good looks like: runtime observation and source evidence remain distinct authority classes; side-effecting Burp actions stay explicit.

## Reflection evaluation

Do not score hidden chain-of-thought. Record only structural outcomes around reflection triggers.

Candidate trigger types:

- `hypothesis_promotion`
- `universal_claim`
- `negative_evidence`
- `contradiction`
- `verification_declaration`
- `branch_abandonment`
- `final_conclusion`

For each checkpoint, record:

```text
trigger
claim/reference before
claim/reference after
new evidence requested: yes/no
contradiction found: yes/no
gap discovered: yes/no
outcome changed: yes/no
extra tool calls
wall-clock / token-cost proxy if available
useful_change: yes/no
```

The goal is to identify reflection triggers that materially change conclusions. Remove or weaken triggers that mostly add latency/context without improving outcomes.

## Journey scorecard

For every journey, evaluate:

### Correctness

- Did the agent identify the relevant architecture?
- Did it inspect exact implementation before strong behavioral claims?
- Did it distinguish static possibility from runtime proof?
- Did it avoid universal conclusions from negative search evidence?
- Were contradictions handled honestly?

### Retrieval value

- Did indexed/semantic discovery find something native lexical search likely would not?
- Did reranking improve useful candidate ordering?
- Were retrieval calls duplicated unnecessarily?
- Did the agent fall back appropriately when a backend degraded?

### Epistemic quality

- Useful hypotheses created?
- Gaps explicit?
- Findings promoted only when justified?
- Authority classes preserved?
- Reflection changed any incorrect/over-broad conclusion?

### Continuity

- Did automatic compaction happen?
- Did important investigation state survive?
- Did the user need to repeat context?
- Did the agent re-run retrieval just to reconstruct lost state?
- Did references remain useful after compaction?

### UX

- Could the user stay in natural language?
- Were IDs mostly invisible until useful?
- Did Awoki ask unnecessary schema/protocol questions?
- Were “why saved” labels understandable?
- Was ambiguity handled without becoming annoying?

### Cost / complexity

- Number of MCP calls.
- Duplicate calls.
- Persisted durable objects created.
- Persisted objects later reused.
- Mechanisms exercised.
- Mechanisms that added no observed value.
- Any state that became stale/confusing.

## Feature usefulness audit

After 10–20 journeys, review each persistent mechanism using this table:

| Mechanism | Failure prevented | Real journeys used | Observable value | User friction | State cost | Overlap | Decision |
|---|---|---:|---|---|---|---|---|
| Example: human reference labels | lost/opaque IDs | J9,J10 | high | low | low | some overlap with summaries | keep |

Allowed decisions:

- **KEEP** — clear repeated value;
- **SIMPLIFY** — valuable but too much surface/state;
- **MERGE** — overlapping mechanism should become one concept;
- **EXPERIMENTAL** — insufficient real evidence;
- **REMOVE** — testable mechanism that does not earn its cost.

## Complexity budget for new mechanisms

Do not add a new persistent concept during stabilization unless a real journey exposes a failure that cannot be fixed more simply.

Any proposed addition must state:

1. concrete observed failure;
2. affected journey(s);
3. why existing mechanisms cannot solve it;
4. expected user-visible benefit;
5. new durable state and lifecycle;
6. stale/ambiguity behavior;
7. regression tests;
8. what complexity can be removed or avoided in exchange.

## Suggested comparison baseline

For selected journeys, compare Awoki against a deliberately simpler agent configuration:

- same model;
- same repository;
- native OpenCode source tools/Grep;
- no semantic index or durable epistemic graph beyond normal conversation;
- same time/tool budget when practical.

Do not expect the baseline to be worse everywhere. The purpose is to identify where Awoki creates real advantage and where it only adds overhead.

## Release decision after stabilization

Do not call the stabilization phase successful merely because all internal tests remain green.

A strong outcome should demonstrate:

- several real investigations where Awoki prevents a material reasoning/provenance mistake;
- compaction recovery that users do not need to micromanage;
- reflection triggers with measurable value;
- natural reference navigation that is actually reused;
- reduced/merged/removable mechanisms identified and acted on;
- normal security work that remains conversational instead of schema-driven.

The expected deliverable is a **smaller or clearer Awoki**, not necessarily a larger one.
