# Aixion Control Tower

Aixion Control Tower is a mobile-first AI project execution cockpit for serious builders who want AI agents to move work forward without giving them uncontrolled access to code, infrastructure, or critical decisions.

It captures raw ideas from mobile, converts them into structured project plans, creates agent work orders, reviews proposed file changes, scores risk, enforces approval policies, triggers tests, tracks execution, and safely applies approved changes through GitHub branches and pull requests.

> Think from your phone. Let agents work. Approve with evidence. Ship with control.

## Why This Exists

AI coding tools are good at producing code, but the workflow around them is still weak. Developers either stay tied to a laptop to approve every change, or they give agents too much autonomy and hope nothing breaks.

Aixion Control Tower is the missing control layer:

- mobile idea capture
- project brain and memory
- agent work orders
- risk-based approval
- mobile diff review
- test gating
- GitHub branch and PR workflow
- audit trail for every agent action

## Product Positioning

Aixion Control Tower is not a ChatGPT wrapper and not a generic GitHub mobile client. It is a human-in-the-loop control tower for AI-assisted software execution.

The core principle is simple:

> AI-generated work should be controlled by risk, evidence, tests, rollback plans, and traceability.

## MVP Goal

The MVP proves one end-to-end workflow:

1. Capture an idea.
2. Generate a project spec.
3. Create an agent work order.
4. Receive a proposed file change.
5. Score the risk.
6. Review the diff.
7. Approve, reject, or request revision.
8. Apply approved changes to a branch.
9. Run tests.
10. Store an audit event.

## Non-Negotiable Rules

- No direct `main` branch edits by agents.
- No credential or secret file edits from mobile.
- No high-risk approval without a diff.
- No critical approval without a test plan and rollback plan.
- No patch application without audit logging.
- No silent agent actions.
- No auto-merge in the MVP.

## Repository Structure

```text
backend/      FastAPI MVP backend for projects, ideas, work orders, approvals, risk scoring, and audit logs
mobile/       Android/mobile app specification and future React Native implementation
agent-runner/ Future worker layer for patch application, test execution, and GitHub workflows
docs/         Product scope, MVP spec, architecture, risk model, approval engine, and elite roadmap
examples/     Sample approval requests and work orders
```

## Documentation

Start here:

- [Product Scope](docs/PRODUCT_SCOPE.md)
- [MVP Specification](docs/MVP_SPEC.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Approval Engine](docs/APPROVAL_ENGINE.md)
- [Risk Engine](docs/RISK_ENGINE.md)
- [Security Model](docs/SECURITY_MODEL.md)
- [Elite Roadmap](docs/ELITE_ROADMAP.md)

## Current Status

Phase: MVP foundation.

The first build focuses on backend workflow correctness before adding complex agent autonomy.
