# Current Aixion Control Tower Context

## Identity

Aixion Control Tower is a flagship Aixion Lab system for controlled AI-assisted software execution.

Its core value is not "agents can act." Its value is that agents can move work forward only inside approval, validation, rollback, scope, authentication, and audit boundaries.

## Current repository status

The repository describes itself as release-candidate / demo-ready, with remaining work centered on release validation, deployment rehearsal, real-device testing, packaging, Play Store readiness, and UX polish.

That means agents should not invent new core features as a default response to bugs or release work.

## Non-negotiable invariants

- No direct main-branch edits by agents.
- No auto-merge in the current release model.
- No silent mutating MCP actions.
- No external-agent action outside configured scope.
- No unauthenticated connector execution.
- No replay acceptance inside protected nonce/timestamp rules.
- No PR creation from approved-worker flow if validation fails.
- Unsafe/missing production configuration must fail closed.
- Every mutating path must remain auditable.
- Approval is a security boundary, not UI decoration.

## Context-loading policy

For an adapter/connector task, load:
- this file;
- `.aixion/project.yaml`;
- exact connector/adapter code;
- exact connector contract docs;
- exact tests;
- related failure records.

For mobile approval UX, load:
- relevant Android screen/view-model/API mapping;
- approval contract;
- focused tests.

For release validation, load:
- `docs/FINAL_RELEASE_CHECKLIST.md`;
- only the subsystem docs needed by a failing gate.

Do not load all backend, mobile, connector, and roadmap documents into every task.

## Engineering loop

task packet
-> scope/risk classification
-> implementation
-> focused tests
-> backend/Android verification as applicable
-> end-to-end or security-path verification when applicable
-> evidence record
-> PR

## Adapter/approval verification minimum

Where relevant, prove:
- valid request accepted;
- invalid/auth-failed request rejected;
- approve path;
- reject path;
- timeout/cancel path;
- replay path rejected;
- scope violation rejected;
- validation failure blocks downstream execution;
- audit record captures the decision.

## Session-end extraction

Persist only:
- architecture decision;
- proven fix;
- failure/root cause;
- changed security invariant;
- reusable validation command;
- concrete evidence pointer.

Do not persist raw conversational history.
