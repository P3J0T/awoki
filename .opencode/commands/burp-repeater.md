---
description: Explicitly copy a clearly identified Burp request into Repeater
---

Load the `burp-workflow` skill. Treat `$ARGUMENTS` as explicit intent to create or update a Repeater tab, but not to send the request.

Resolve the request from the active Burp editor, a live history item, or an Awoki saved request reference. If more than one request matches, ask the user to choose. Use direct Burp MCP for live requests; use the Awoki archive helper only for saved-run material.

Report the source request, destination tab name, protocol, and whether any request bytes were changed. Do not transmit the request merely because it was placed in Repeater.
