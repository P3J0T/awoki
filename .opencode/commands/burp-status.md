---
description: Check live Burp connectivity and Awoki Burp evidence state without changing traffic
---

Load the `burp-workflow` skill.

Perform a read-only status check:

1. Inspect whether the direct PortSwigger Burp MCP server is reachable and list the live capabilities actually available.
2. Call the Awoki Burp status helper for saved-run/evidence state when relevant.
3. Report the effective live URL/target without exposing credentials, the latest saved run, attached project, and any degraded or unavailable component.

Do not send requests, mutate the active editor, create Repeater tabs, or stage Intruder requests during a status check.
