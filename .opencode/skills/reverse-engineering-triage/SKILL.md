---
name: reverse-engineering-triage
description: Initial static triage workflow for unknown binaries, firmware, suspicious files, or reverse-engineering artifacts.
compatibility: opencode
metadata:
  scope: project
  version: "1"
  tags: reverse-engineering,malware,firmware,triage,security
---

# Reverse Engineering Triage

## Safety

Prefer static analysis before dynamic execution. Ask before executing unknown binaries, attaching debuggers, enabling network, or running destructive commands.

## Procedure

1. Identify file type, hash, architecture, entropy, symbols, imports, sections.
2. Extract strings and obvious IOCs.
3. Identify entrypoints and high-level control flow.
4. Search project memory for prior notes on this sample/family.
5. Search artifacts for matching strings, hashes, functions, or paths.
6. Record confirmed facts with evidence.
7. Record uncertain ideas as hypotheses.

## Evidence Standard

Each meaningful claim should include:

- file path,
- function/address/offset when available,
- observed string/import/symbol/log,
- confidence: confirmed | likely | hypothesis.

## Output

- Confirmed facts
- Hypotheses
- Evidence table
- Risk notes
- Next steps
