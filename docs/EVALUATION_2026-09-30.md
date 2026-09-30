# Investigation continuity evaluation — 2026-09-30

Awoki v0.2.2 strengthens saved-state and recovery safeguards. The tests below do
not establish reliable unattended investigation continuity: Qwen sometimes failed
to save important instructions, read recovery references or preserve uncertainty.

## Implementation checks

The implementation snapshot passed **886 tests, zero failures, errors or
skips**, using Python 3.12.14 and the cached Linux runtime. Supporting checks
passed for source policy, dependencies, parser behavior and retrieval contracts.
The continuity plugin passed type and runtime checks against OpenCode SDK/plugin
versions 1.18.30 and 1.14.33. These checks establish tested implementation behavior,
not the correctness of model-authored findings.

The initial validated snapshot was compared by hash across 250 source files.
Release metadata and this evaluation document were added afterward. The first
release CI run also exposed a portable TypeScript smoke-check incompatibility:
without SDK types, optional-chain equality did not prove that cached recovery
state existed. Explicit presence guards replace those checks without changing the
intended recovery policy. Release validation and GitHub CI are separate checks;
the live model results below predate this compiler-compatibility correction.

## Live setup

Native OpenCode 1.14.48 called actual Awoki MCP tools and
`qwen3.8-27b-turbo` through an authorized OpenAI-compatible endpoint. Each run used
isolated state and small synthetic Python repositories. The MCP runtime had no
network access; the native model process owned inference. Source editing and
execution were denied. No private production repository, embedding endpoint or
reranking endpoint was part of this evaluation.

The fixtures covered callback ordering for an empty cart, pickup-code normalization
and retry boundaries. Scenarios exercised ordinary exploration, saved direction,
correction, changed intent, corrupted state, new-session handoff, bounded TODO
state, manual compaction/restart and native automatic compaction.

## Results

| Run | Observed result |
| --- | --- |
| Initial eight-scenario protocol | All eight missed at least one requirement; 13 assertions passed, 18 failed and one was inconclusive. |
| Revised recovery protocol, eight scenarios | One scenario passed fully; seven missed at least one requirement. Assertions: 17 passed, 15 failed. |
| Final three-deliverable calibration | Four assertions passed and three failed. Source answers, initial pause and compact/restart behavior passed; saved limits/scope, linked native TODOs and exact checkpoint recall did not. |
| Final automatic-pressure retest | Initial automatic replay and recovery-packet delivery were observed. A second compaction triggered the bounded abort, so the mechanical test was inconclusive. Before abort, the model incorrectly claimed no work had started. |

The revised protocol allowed the supplied recovery snapshot to replace an initial
work-status call; exact note recovery requirements remained. This is an explicitly
changed protocol, not a controlled statistical comparison. Earlier results remain
failures. The final calibration and pressure retest used the final prompt and
packet wording; the earlier eight-scenario runs predated that wording.

## What the tests established

- Exact checkpoint and handoff reads worked in several runs. A no-goal lookup
  completed correctly with the delivered recovery snapshot.
- A real search-integrity defect was fixed: corrupt canonical memory now returns
  unknown instead of successful empty results or index refresh. A live fault case
  preserved the corrupted files and correctly reported unavailable memory.
- Stored TODO references and omission counts survived the tested large-ledger
  case. The model still failed to account for omitted work in its conclusion.
- Correct source answers did not imply complete durable continuity. In the final
  calibration, the checkpoint omitted the user's no-edit/no-execution limits and
  file scope even though the model respected those limits during that run.
- Correct recall did not establish correct interpretation. An earlier resumed
  run read a caveat that design intent was unknown, then saved a confident claim
  that the behavior was intended without evidence establishing that intent.

## Boundaries

Automatic pressure was induced by reducing the declared context limit around real
provider-reported usage. It was not a full 210,000-token capacity test. The large
TODO ledger was injected as test state, not produced by a live 65-row native TODO
event. Unchanged fixture files reflect enforced permissions and do not prove the
model would never propose edits. These are diagnostic single-run cases, not
production failure rates or a full live test of OpenCode 1.18.30.

For consequential investigations, inspect saved goals and constraints, read exact
checkpoint references after resuming, and preserve uncertainty unless new evidence
resolves it. Recovery delivery and storage integrity cannot certify those model
decisions.
