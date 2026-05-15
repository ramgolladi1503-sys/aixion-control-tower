# Aixion Control Tower

Aixion Control Tower is a mobile-first approval and execution control plane for AI-assisted software work.

It lets builders capture ideas, turn them into structured work, route AI-generated changes through mobile approval, gate risky execution, validate approved work, and open GitHub pull requests without giving agents uncontrolled access to critical systems.

> Think from your phone. Let agents work. Approve with evidence. Ship with control.

## What It Is

Aixion Control Tower is not a ChatGPT wrapper and not a generic GitHub mobile client.

It is a human-in-the-loop control tower for:

```text
AI-generated code changes
GitHub branch and PR execution
MCP mutating tool-call approval
external-agent task intake
mobile approval and audit trails
```

The product principle is simple:

```text
Agents can move work forward, but execution must stay inside approval, validation, rollback, and audit boundaries.
```

## Core Capabilities

```text
mobile project/work-order control
approval inbox and diff review
risk-aware approve/reject/revise flow
GitHub branch/file/PR worker path
approved AgentTask worker orchestration
retry and cancellation controls
MCP gateway wait-mode approval queue
connector registry for external agents
scoped external AgentTask access
generic inbound connector webhook
connector schema mapper
connector templates for OpenClaw, Antigravity, Gemini/custom, Claude/Cursor, and local bridge
Android connector management screen
connector simulator for safe sample payload testing
HMAC v1 callback hardening
containerized validation runner with fail-closed behavior
role/session/invite owner controls
audit-first execution model
runtime readiness checks
```

## End-to-End Flow

```text
idea / external agent / MCP request
-> structured task or approval request
-> risk and scope controls
-> mobile review
-> approve / reject / revise
-> branch creation
-> file application
-> isolated workspace
-> containerized validation
-> pull request opened for human review
-> audit trail retained
```

## Non-Negotiable Rules

```text
No direct main branch edits by agents.
No auto-merge in the MVP/release candidate.
No silent mutating MCP tool calls.
No external-agent action outside its configured scope.
No connector webhook execution without authentication.
No HMAC callback replay within the nonce window.
No approved worker PR creation if validation fails.
No production startup with unsafe missing configuration.
```

## Repository Structure

```text
backend/      FastAPI backend: approvals, agents, MCP gateway, connectors, GitHub worker, audit, runtime readiness
mobile/       Android app: approvals, diff review, agent tasks, MCP queue, connectors, owner controls
docs/         Product, architecture, validation, release, connector, and deployment documentation
examples/     Sample approval/work-order artifacts where applicable
```

## Important Documentation

Start here:

```text
docs/DEMO_SCRIPT.md
docs/FINAL_RELEASE_CHECKLIST.md
docs/PLAY_STORE_READINESS.md
docs/CONNECTED_AGENT_SCOPE.md
docs/CONNECTOR_TEMPLATES.md
docs/CONNECTOR_SCHEMA_MAPPER.md
docs/CONNECTOR_SIMULATOR.md
docs/PUBLIC_CONNECTOR_CALLBACK_HARDENING.md
docs/CONTAINERIZED_VALIDATION_RUNNER.md
```

Legacy/product docs remain useful for background:

```text
docs/PRODUCT_SCOPE.md
docs/MVP_SPEC.md
docs/ARCHITECTURE.md
docs/APPROVAL_ENGINE.md
docs/RISK_ENGINE.md
docs/SECURITY_MODEL.md
docs/ELITE_ROADMAP.md
```

## Release Status

```text
Status: release-candidate / demo-ready control tower
Backend P0: effectively complete
Android MVP control flow: effectively complete
Configurable external-agent platform: effectively complete
Remaining work: release validation, deployment rehearsal, real-device testing, packaging, Play Store readiness, and UX polish
```

## What Not To Overclaim

Aixion Control Tower is strong release-candidate software, but it is not magic.

```text
It is not a high-assurance sandbox for arbitrary untrusted code.
It is not enterprise-grade vault-backed credential management yet.
It is not a replacement for real CI/CD.
It is not a fully autonomous merge bot.
It is not Play Store-ready until packaging, privacy, policy, signing, and backend deployment are complete.
```

## Demo Positioning

Use this line:

```text
Aixion lets agents move fast, but only inside a controlled approval, validation, and audit boundary.
```
