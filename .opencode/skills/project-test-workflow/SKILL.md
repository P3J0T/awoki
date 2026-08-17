---
name: project-test-workflow
description: Discover and run project tests while respecting local continuity, explicit sensitive-memory requests, and safety boundaries.
compatibility: opencode
metadata:
  scope: project
  version: "2"
  tags: testing,qa,project,reliability
---

# Project Test Workflow

## Procedure

1. Call `recall_context` with "test workflow commands and environment requirements".
2. Inspect project files for test runners: Makefile, package.json, pyproject.toml, cargo.toml, go.mod, pytest.ini.
3. Prefer project-local command memory over global defaults.
4. If sensitive values are required, follow the user's explicit instruction and the available project skill or external mechanism; do not invent a credential backend.
5. Ask before running commands that hit external services unless the user already authorized that scope.
6. Run the smallest relevant test first, then broader tests.
7. Save reusable project test facts with `save_project_fact`.
8. Propose global promotion only for generalized testing heuristics.

## Output

- command chosen,
- why it was chosen,
- results,
- failures,
- saved project facts,
- promotion candidates if any.
