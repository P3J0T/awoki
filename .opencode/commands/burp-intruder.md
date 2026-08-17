---
description: Explicitly stage a clearly identified Burp request in Intruder without launching an attack
---

Load the `burp-workflow` skill. Treat `$ARGUMENTS` as explicit intent to send the selected request to Intruder.

Resolve the request unambiguously from the active editor, live history, or a saved request reference. If ambiguous, ask one concise question. Stage the request through direct Burp MCP when live; use the archive helper only for saved-run evidence.

This command does not authorize starting an Intruder attack, configuring payloads, active scanning, or sending repeated traffic. Report what was staged and stop.
