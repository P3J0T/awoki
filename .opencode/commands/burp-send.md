---
description: Explicitly send one clearly identified HTTP request through Burp
---

Load the `burp-workflow` skill. This command represents explicit intent for one network send, but the request and target must still be unambiguous.

Before sending, identify the request source, method, target host, path, HTTP version, and any deliberate mutation. Use direct PortSwigger Burp MCP. Ask one concise question if the active request or target is unclear. Do not repeat a failed send automatically and do not escalate to Intruder, scanner, or repeated replay.

After the send, report the observed status/error and preserve only a compact sanitized project observation when requested.
