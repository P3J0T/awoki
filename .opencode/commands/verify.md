---
description: Verify important claims against actual evidence without running a full delivery gate
---

Load the `reliability-verification` skill for: $ARGUMENTS

For findings/conclusions the user wants reviewed before relying on them, follow
its Focused findings review using the existing reliability records. Read
`reliability_status(view="review")` before answering and preserve report links
and unknowns in continuity; never certify a paraphrase from a receipt. For a
simple factual question, use the lighter Verify workflow without a formal run.

Use the smallest adequate checks. Search for contradictory project memory. For Burp-derived claims, load `burp-workflow` and use direct Burp MCP for live state. Report observed facts, inference, uncertainty, and anything not verified.
