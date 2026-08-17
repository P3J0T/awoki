---
description: Review pending local-to-global promotion candidates
---

Call `list_promotion_candidates`. For each candidate, decide whether it is safe to globalize. Do not approve candidates containing secrets, client names, private endpoints, project-specific hashes, or un-generalized local paths.


Approved/rejected candidates are resolved append-only in `.harness/memory/promotion_candidates.jsonl`. If a previously global fact is not globally valid, call `demote_global_memory` with the global memory line number and a reason.
