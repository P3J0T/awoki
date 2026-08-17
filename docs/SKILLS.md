# Skills and Recipes

Awoki uses OpenCode-native skills as reviewed, on-demand procedure files.

```text
Memory/RAG  = what Awoki knows
SKILL.md    = how Awoki should perform a repeatable workflow
MCP tool    = callable action
AGENTS.md   = short standing rules
Command     = explicit user entry point
```

## Location

Project skills:

```text
.opencode/skills/<skill-name>/SKILL.md
```

Global skills:

```text
~/.config/opencode/skills/<skill-name>/SKILL.md
```

OpenCode recognizes `name`, `description`, optional `license`, optional `compatibility`, and string-valued `metadata` in skill frontmatter.

Example:

```yaml
---
name: reliability-verification
description: Verify important claims or run an adaptive local reliability gate.
compatibility: opencode
metadata:
  scope: project
  workflow: verify-reliability-ship
---
```

## Selection flow

```text
user task
  -> OpenCode native skill discovery or Awoki search_skills
  -> load_skill(skill_name)
  -> recall project context when historical facts are needed
  -> use required tools
  -> capture durable findings or propose generalized lessons
```

Examples:

```text
"analyze this unknown binary"     -> reverse-engineering-triage
"verify these conclusions"        -> reliability-verification
"work with live Burp state"       -> burp-workflow
"remember this as reusable"       -> scoped-memory
"create a repeated procedure"     -> skill-authoring
"prime this repo for code review" -> repository-readiness
```

Awoki has no built-in credential skill. A user may later add a Keychain, Bitwarden, or custom credential skill in the same correct folder layout.

## Skill contents

A useful skill includes purpose, selection criteria, preconditions, allowed tools, safety gates, procedure, outputs, and failure modes. It should not contain project facts, raw evidence dumps, or embedded secret values.

## Proposals

`propose_skill_update` writes a review candidate to the ignored runtime file `.harness/memory/skill_update_candidates.jsonl`. It does not auto-edit or auto-enable skills.

## Repository readiness

The built-in `repository-readiness` skill coordinates a managed repository from
passive readiness inspection through detached structural indexing, explicit semantic
vector materialization, and final backend verification. It distinguishes `LOCAL_READY`
from `FULL_READY`, treats full semantic readiness as explicit remote-embedding intent,
and never edits runtime configuration or tight-polls detached jobs. Natural language
such as `prepare this repo for code review` or `/project prime oathkeeper for full
retrieval` selects this procedure without adding another one-to-one slash command.
