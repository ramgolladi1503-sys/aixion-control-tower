# Aixion Control Tower — Aixion Harness

This directory is the compact operating context for agent-assisted engineering in Aixion Control Tower.

Its purpose is to let Codex/Claude/other workers receive small, bounded task packets instead of the full repository or prior chat history.

Load order:

1. `project.yaml`
2. `context.md`
3. `task-packet-template.yaml`
4. relevant decisions/failures/evidence only
5. affected source/tests/docs

Rules:
- repository state is authoritative;
- approval boundaries are not optimization targets;
- no direct-main agent edits;
- no silent mutating MCP/tool actions;
- no validation bypass;
- no scope expansion;
- every mutating path must preserve auditability and fail closed;
- persist durable lessons, not transcripts.
