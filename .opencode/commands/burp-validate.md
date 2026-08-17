---
description: Run a read-only end-to-end validation of Burp MCP and Awoki Burp evidence integration
---

Load the `burp-workflow` skill.

Validate, without transmitting traffic:

1. direct Burp MCP connectivity and tool discovery;
2. read-only access to a harmless live source when available, such as a bounded history page or active-editor metadata;
3. Awoki saved-run parser/status validation;
4. project evidence paths and exclusion of raw traffic from broad RAG.

Report each observed check separately. Do not send requests, create Repeater tabs, stage Intruder requests, start scans, or claim a capability passed unless its result was observed.
