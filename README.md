# Aixion Control Tower

Aixion Control Tower is an approval console for AI agents working on a user's
Mac. The first product flow connects an Antigravity session to the Android app:
the Mac pauses a sensitive tool call, the phone presents the exact request, and
the same Mac session resumes only after the authenticated approval is recorded
and verified.

The first release is focused on that approval loop. GitHub workers, MCP requests,
connectors, and other control-plane capabilities remain separate product paths;
their presence in this repository does not certify them for production use.

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

## Target Approval Flow

```text
Mac Antigravity tool call
-> authenticated approval request bound to the exact invocation
-> Android inbox and detail review
-> one durable, audited approve or deny decision
-> Mac hook verifies that same decision and payload
-> resume only on verified approval; deny on timeout or failure
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
docs/CONNECTED_AGENT_INTERACTION_ROADMAP.md
docs/CONNECTED_AGENT_SCOPE.md
docs/PHASE0_LAN_VALIDATION.md
docs/PHASE0_REAL_DEVICE_SMOKE_VALIDATION.md
docs/ANDROID_DEBUG_APK_GITHUB_ACTIONS.md
docs/EXTERNAL_AGENT_SETUP_VALIDATION.md
docs/CONNECTOR_E2E_VALIDATION.md
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

The Antigravity approval flow has completed one controlled physical-device
approve round trip using a local backend and USB reverse. The hook and Android
app have focused tests, but this is not production certification.

Production remains blocked on transactional PostgreSQL persistence and
multi-process safety, a durable notification outbox and FCM HTTP v1, hosted HTTPS
deployment and recovery drills, and real-device denial and failure-path
certification. Android packaging and release validation are also incomplete.
See [the production readiness plan](docs/PRODUCTION_READINESS_PLAN.md) for the
current evidence and release gates. Do not use this build as a public production
approval service.

## What Not To Overclaim

The controlled local pilot demonstrates the approval loop, but production readiness is still blocked by the items above.

```text
It is not a high-assurance sandbox for arbitrary untrusted code.
It is not enterprise-grade vault-backed credential management yet.
It is not a replacement for real CI/CD.
It is not a fully autonomous merge bot.
It is not Play Store-ready until packaging, privacy, policy, signing, and backend deployment are complete.
```

## Demo Positioning

For a controlled pilot, use this line:

```text
Aixion routes a Mac agent's sensitive actions to your phone for explicit review.
```
