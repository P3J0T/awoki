---
name: burp-workflow
description: Use when inspecting, controlling, summarizing, preserving, or resuming Burp Suite work. Prefer direct PortSwigger Burp MCP for live actions; preserve meaningful results through Awoki project continuity and safe summaries.
compatibility: opencode
metadata:
  scope: project
  version: "3"
  tags: burp,web-security,proxy-history,repeater,intruder,http,evidence,handoff,request-replay
---

# Burp Workflow

## Core architecture

Awoki uses a hybrid Burp model:

- Direct PortSwigger Burp MCP is the live Burp control plane.
- Awoki is the continuity, safe evidence-summary, handoff, and retrieval layer.

Do not make Awoki Python wrappers replace Burp MCP for ordinary live work.
Do not load or apply this skill merely because repository code contains HTTP endpoints,
authentication, tokens, cookies, credentials, JWT/OAuth, or security terminology. Those are
ordinary code-analysis subjects. Use this skill only when the user asks about Burp, live/saved
Burp traffic, or an explicitly Burp-backed web-security workflow.

## User interface

Use `/burp` or ordinary natural language for read-only inspection, searching, summarization, saved-run lookup, and safe preservation. Keep these explicit commands only where side-effect intent matters:

```text
/burp-repeater <request>
/burp-intruder <request>
/burp-send <request>
/burp-status
/burp-validate
```

`/burp-intruder` stages a request but never authorizes launching an attack. `/burp-send` authorizes one unambiguous network send, not repetition, scanning, or escalation.

## Default decision rule

Use direct Burp MCP first for live/current Burp actions:

- inspect proxy HTTP history
- inspect WebSocket history
- inspect Organizer items
- inspect active editor contents
- set active editor contents
- create Repeater tabs
- send to Intruder
- send HTTP/1.1 or HTTP/2 requests through Burp
- URL/base64/random utility operations exposed by Burp MCP

Use Awoki after or around live work to preserve durable project state:

- `burp_record_observation` for a compact sourced observation
- `burp_save_host_summary` for a sanitized registered summary
- `project_capture` for findings, decisions, questions, corrections, and reflections
- `project_pause` when stopping or switching direction
- `project_search` and `project_open` when resuming

Burp task tools remain compatibility adapters. They must write to the same canonical
continuity journal and must not become the required resume path.

Use Awoki Python pull/find/report commands only for offline/archive/bulk work against saved Awoki Burp runs.

## PortSwigger MCP capabilities to rely on

PortSwigger Burp MCP exposes live tools for proxy HTTP history, regex-filtered history, WebSocket history, Organizer items, active editor get/set, sending HTTP/1.1 and HTTP/2 requests, creating Repeater tabs, sending to Intruder, utilities, and some Professional features such as scanner issues and Collaborator tools when available.

MCP history/Organizer tools are paginated and may return serialized text items. Very large serialized history items may be truncated. Do not manually parse raw MCP JSON unless absolutely necessary.

## Domain/hostname work

For prompts like:

- "check everything about this hostname"
- "search this domain in Burp"
- "what do we know about example.com?"

Do a two-phase workflow:

1. Live discovery through direct Burp MCP.
2. Project preservation through Awoki.

Minimum coverage expectations before answering:

- live Burp checked: yes/no
- saved Awoki evidence checked: yes/no
- project evidence checked: yes/no
- global evidence checked: yes/no
- raw match count before dedup
- unique endpoint count after grouping
- dedup policy
- what was not scanned
- confidence

Search/match broadly for host/domain evidence:

- URL
- Host header
- request line
- all request headers
- request body snippets when available
- response headers
- response body preview/snippets when available
- Referer
- Origin
- Location redirects
- JavaScript URLs and embedded references

Do not say "only N requests" unless coverage is explicit.

## Recording results into Awoki

After direct Burp MCP work finds something worth keeping, record it with Awoki.

For one finding or request observation:

```text
burp_record_observation(project, title, summary, host, method, path, status_code, request_ref, artifact, next_action)
```

For a host/domain summary:

```text
burp_save_host_summary(project, hostname, summary, coverage, request_refs, next_action)
```

For long Burp analysis, periodically capture one concise operational reflection:

```text
project_capture(
  name=project,
  kind="continuity_reflection",
  summary="What changed or was established",
  sources=[safe references],
  uncertainty=[unverified points],
  likely_continuation="Suggestion only"
)
```

Use `project_task_checkpoint`, `project_task_status`, and `project_task_finalize` for generic repository/document/research work. `burp_task_checkpoint`, `burp_task_status`, and `burp_task_finalize` are scoped to live Burp workflows and remain only as Burp compatibility helpers; do not use them merely because a task needs checkpointing. On resume, use `project_open`; the
user's new instruction overrides any stored next action.

## Project storage model

Project Burp files are **optional and lazy**. A generic project has no Burp artifact
tree. The following directory is created only after explicit Burp preservation/write activity:

```text
workspace/projects/<project>/artifacts/burp/
```

Read-only project open/status/search must not create it.

Important compatibility and evidence files:

```text
runs.jsonl              saved Awoki Burp run pointers
observations.jsonl      compact adapter records
host-summaries.jsonl    compact host/domain summaries
tasks/                  optional compatibility checkpoints
latest.md               latest linked saved runs
handoff.md              compact Burp adapter handoff
host-reports/           registered RAG-safe summaries/reports
extracted/              deliberate raw request artifacts; never broadly indexed
```

Raw Burp evidence remains in global run folders or inside Burp itself. Raw traffic is not broadly loaded into chat or RAG.

## Internal saved-run/archive helpers

These remain available to the skill and maintainers for saved-run/archive work. They are intentionally not exposed one-for-one as slash commands:

```bash
/awoki/.harness/bin/awoki-runtime-env --profile burp -- \
  python .harness/integrations/burp/awoki_burp.py pull-history --project-related <project>
/awoki/.harness/bin/awoki-runtime-env --profile burp -- \
  python .harness/integrations/burp/awoki_burp.py pull-history-regex --regex "api|auth|login" --project-related <project>
/awoki/.harness/bin/awoki-runtime-env --profile burp -- \
  python .harness/integrations/burp/awoki_burp.py find-request --pattern "..." --project-related <project>
/awoki/.harness/bin/awoki-runtime-env --profile burp -- \
  python .harness/integrations/burp/awoki_burp.py host-report --hostname example.com --project-related <project>
```

These are fallback/archive helpers only. Live Burp remains the direct `mcp.burp`
control plane; do not route live Burp actions through the generic runtime wrapper.
The `burp` child environment does not receive embedding/reranker API-key variables. This is not a sandbox against hostile code already running as the same `op` user.

Use them when Burp MCP live output is too large, when Burp is offline and saved evidence is needed, or when rebuilding project reports.

## Anti-loop rules for local LLMs

- Do not repeat the same failed Burp command twice.
- Do not parse raw Burp MCP JSON manually when a live Burp MCP tool or Awoki evidence tool can answer.
- Do not summarize grouped endpoints as request count.
- Do not deduplicate before reporting raw match count.
- If results are surprisingly small, automatically run one broader verification pass.
- If output is large, save a compact continuity reflection and resume through `project_open` instead of relying on context.
- After two failed attempts, stop and report searched sources, not-scanned sources, and exact next command/tool.
- Active network sends, Repeater creation, and Intruder sends require explicit user intent.

## RAG policy

Index only canonical safe continuity and explicitly registered sanitized Burp
summaries, such as `host-reports/*.md`. Compatibility JSONL/task files are not
implicitly trusted merely because they are compact.

Do not index or dump broadly:

- raw request/response material or extracted `.http` files
- full HTTP or WebSocket history
- credential, cookie, or token values
- exported environment files
- arbitrary adapter metadata not normalized into continuity
