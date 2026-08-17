---
description: Inspect, search, summarize, preserve, or explicitly control Burp using natural language
---

Load the `burp-workflow` skill and interpret `$ARGUMENTS` as the user's Burp intent.

Use direct PortSwigger Burp MCP for live/current state and actions. Use Awoki only for compact project observations, sanitized host summaries, continuity, saved-run/archive work, and handoff.

Natural read-only or preservation requests may route automatically, for example:

```text
/burp show the latest login request
/burp find requests to api.example.com
/burp summarize everything observed for example.com
/burp save this request as a project observation
/burp inspect saved Burp runs for the callback endpoint
```

Side-effect rules:

- Network sends, active-editor mutation, Repeater creation, and staging into Intruder require explicit user intent in this request.
- Never start an Intruder attack, active scan, Collaborator interaction, or repeated replay unless explicitly requested.
- If the request identity, target, protocol, or mutation is ambiguous, ask one concise question instead of choosing.
- Prefer the explicit `/burp-send`, `/burp-repeater`, or `/burp-intruder` commands when the user wants a precise side effect.

Do not manually parse raw MCP JSON when a direct Burp MCP tool or structured Awoki reader exists. Keep raw requests, responses, cookies, tokens, and credentials out of broad project memory and RAG.
