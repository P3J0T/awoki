---
name: skill-authoring
description: Create or update SKILL.md recipes safely and consistently.
compatibility: opencode
metadata:
  scope: project
  version: "1"
  tags: skills,recipes,procedures,governance
---

# Skill Authoring

## Purpose

Turn repeated procedures into reviewed, versioned skills.

## Rules

- Skills are operational instruction, not passive documentation.
- Do not auto-enable unreviewed third-party skills.
- Do not put plaintext secrets in skills.
- Include preconditions, safety gates, tools, output format, and failure modes.

## Structure

Each skill lives at:

```text
.opencode/skills/<name>/SKILL.md
```

Use YAML frontmatter with:

- `name`
- `description`
- `tags`
- `scope`
- `version`

## Procedure

1. Determine whether the content is a procedure. If not, save as memory instead.
2. Check for an existing project or global skill.
3. Draft or update SKILL.md.
4. Keep the skill short enough to load on demand.
5. Add examples only when they reduce ambiguity.
6. Save reusable facts separately in memory.
