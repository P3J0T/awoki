# Awoki Burp Integration

Awoki uses a hybrid Burp architecture.

```text
PortSwigger Burp MCP = live Burp control plane
Awoki = project evidence, memory, handoff, task, and RAG layer
```

This is intentional. Burp MCP should do live Burp operations. Awoki should not reimplement Burp. Awoki records what mattered, writes compact project artifacts, and makes work resumable.

## Why this design

Direct Burp MCP is better for live work:

```text
inspect proxy history
inspect active editor
create Repeater tabs
send to Intruder
send HTTP/1.1 / HTTP/2 requests
use Burp utilities
use current Burp state
```

Awoki is better for durable project work:

```text
project association
handoff and situation files
RAG-safe summaries
saved observations
pending/continue state
selected request artifacts
long-term evidence pointers
```

Avoid this old pattern:

```text
raw Burp MCP JSON -> ad-hoc Python parsing -> model loops -> partial answer
```

Prefer this pattern:

```text
direct Burp MCP live action -> Awoki compact observation/summary/checkpoint -> project handoff/RAG
```

## OpenCode MCP configuration

`opencode.jsonc` exposes two MCP servers:

```text
mcp.burp   remote PortSwigger Burp MCP server at http://host.docker.internal:9876 with local Host/Origin headers
mcp.awoki  local Awoki harness MCP server
```


The direct Burp MCP config is concrete on purpose; OpenCode does not expand `{env:...}` placeholders in this field reliably:

```jsonc
"burp": {
  "type": "remote",
  "url": "http://host.docker.internal:9876",
  "enabled": true,
  "oauth": false,
  "timeout": 30000,
  "headers": {
    "Host": "127.0.0.1:9876",
    "Origin": "http://127.0.0.1:9876"
  }
}
```

Inside the OpenCode SSH container, Burp MCP URL defaults to:

```text
http://host.docker.internal:9876
```

The Burp MCP extension may accept either:

```text
http://127.0.0.1:9876
http://127.0.0.1:9876/sse
```

depending on client/extension configuration. Awoki container mode uses the Docker-to-host alias.

Environment variables:

```text
AWOKI_BURP_HOST_URL=http://127.0.0.1:9876
AWOKI_BURP_CONTAINER_URL=http://host.docker.internal:9876
AWOKI_BURP_URL=http://host.docker.internal:9876
OpenCode Burp MCP URL=http://host.docker.internal:9876
```

Live Burp is still the direct `mcp.burp` server configured by OpenCode; the generic
runtime wrapper does not proxy or reinterpret live Burp actions. Because direct
OpenCode Burp configuration is concrete, changing the Burp MCP port/URL requires
updating the OpenCode Burp entry as well as the Awoki environment. The `burp` runtime
profile exists only so Awoki's saved-run/archive Python helpers see the same container
endpoint/timeouts after SSH login, without receiving embedding/reranker API-key variables.

## Burp setup

1. Install PortSwigger's Burp MCP Server extension.
2. Enable the MCP server in Burp's MCP tab.
3. Keep the default port `9876` unless you intentionally changed Awoki env/profile.
4. In Burp MCP security settings, allow the data/action permissions you need.

## User command surface

Use `/burp` or ordinary natural language for read-only live inspection, searching, summarization, host review, saved-run lookup, and preservation.

Keep explicit commands only where side-effect intent matters:

```text
/burp-repeater <request>   copy to Repeater without sending
/burp-intruder <request>   stage in Intruder without launching an attack
/burp-send <request>       send one unambiguous request through Burp
/burp-status               read-only connectivity and saved-state status
/burp-validate             read-only end-to-end integration validation
```

The underlying Awoki Python helpers remain internal archive/bulk tools. They are not exposed one-for-one as slash commands.

## Live work: use direct Burp MCP

Use direct Burp MCP for:

```text
proxy HTTP history
regex-filtered proxy HTTP history
WebSocket history
Organizer items
active editor get/set
HTTP/1.1 request send
HTTP/2 request send
create Repeater tab
send request to Intruder
URL/base64/random utilities
scanner issues / Collaborator when available in Burp Professional
```

PortSwigger's history/Organizer tools are paginated and return serialized text items; very large serialized items may be truncated. This is one reason Awoki does not make raw MCP JSON parsing the primary workflow.

## Project evidence: use Awoki tools

After live Burp MCP work, save compact state into the project.

Record one observation:

```text
burp_record_observation(
  project="ASDF-101",
  title="Session bootstrap endpoint observed",
  summary="GET /api/session returns bootstrap metadata and sets csrf cookie.",
  host="app.example.test",
  method="GET",
  path="/api/session",
  status_code="200",
  request_ref="burp-live:...",
  next_action="Test whether csrf is bound to session cookie."
)
```

Save a host summary:

```text
burp_save_host_summary(
  project="ASDF-101",
  hostname="app.example.test",
  summary="Live Burp review found login, session bootstrap, account API, and static JS paths.",
  coverage={"live_burp_checked": true, "raw_matches": 18, "unique_endpoints": 7},
  request_refs=["..."],
  next_action="Open account API request in Repeater."
)
```

Checkpoint long work through generic continuity:

```text
project_capture(
  name="ASDF-101",
  kind="continuity_reflection",
  summary="Grouped host traffic and identified session endpoints.",
  details="18 raw matches, 7 unique endpoints, 2 auth/session indicators.",
  sources=["artifacts/burp/host-reports/app.example.test.md"],
  uncertainty=["Auth binding has not been tested."],
  likely_continuation="Send the selected account API request to Repeater."
)
```

Resume later with `project_open(name="ASDF-101")`. Generic work uses `project_task_checkpoint`, `project_task_status`, and `project_task_finalize`. `burp_task_checkpoint`, `burp_task_status`, and `burp_task_finalize` remain available only for live Burp workflow compatibility and mirror durable meaning into the canonical continuity journal.

## Project files

Project Burp adapter state is optional and created lazily only after explicit Burp preservation/write activity. Generic project creation/opening does not create the Burp tree, and generic global/project recall does not mix saved Burp inventories into unrelated work.

Project Burp adapter state is compact, but only canonical continuity and
explicitly registered sanitized summaries are trusted for RAG:

```text
workspace/projects/<project>/artifacts/burp/
├── runs.jsonl
├── observations.jsonl
├── host-summaries.jsonl
├── latest.md
├── handoff.md
├── host-reports/
├── tasks/
└── extracted/
```

`extracted/` may contain deliberate `.http` working files. Full raw Burp history is not copied into project folders.

## Internal saved-run/archive helpers

Awoki still has Python helpers for offline/bulk/saved-run work:

```bash
/awoki/.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py pull-history --project-related ASDF-101
/awoki/.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py pull-history-regex --regex "api|auth|login" --project-related ASDF-101
/awoki/.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py find-request --pattern "api/session" --project-related ASDF-101
/awoki/.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py show-request --request-ref '<run_id>:req:<id-or-idx-N>'
/awoki/.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py extract-request --request-ref '<run_id>:req:<id-or-idx-N>' --project-related ASDF-101 --name session_bootstrap
/awoki/.harness/bin/awoki-runtime-env --profile burp -- python .harness/integrations/burp/awoki_burp.py host-report --hostname app.example.test --project-related ASDF-101
```

These commands are not the default live workflow anymore. Use them for saved evidence, offline review, rebuilding reports, or deliberate extraction.

## Domain/hostname coverage requirements

For claims about a hostname/domain, include:

```text
live_burp_checked: yes/no
saved_awoki_checked: yes/no
project_checked: yes/no
global_checked: yes/no
raw_match_count_before_dedup: N
unique_endpoint_count_after_grouping: N
dedup_policy: none/grouped/etc
not_scanned: [...]
confidence: high/medium/low
```

Do not say “only N requests” unless this coverage was performed.

## RAG policy

Indexed:

```text
project artifacts/burp/observations.jsonl
project artifacts/burp/host-summaries.jsonl
project artifacts/burp/host-reports/*.md when explicitly registered safe
canonical project continuity records
selected saved-run summaries only after policy review and registration
```

Not indexed broadly:

```text
raw/*.mcp.json
full raw HTTP history
full raw responses
redacted/*.txt by default
credential values
.env files
exported credential env files
```

## Anti-loop rules

- Use Burp MCP directly for live operations.
- Use Awoki to preserve compact observations, safe summaries, and generic project continuity.
- Do not repeat the same failed tool call twice.
- Do not manually parse raw MCP JSON unless all structured/live options fail and the user explicitly wants raw evidence inspection.
- If results look too small, run one broader verification pass before answering.
- If output is large, capture one concise continuity reflection and resume through `project_open`.
