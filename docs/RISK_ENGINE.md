# Risk Engine

## Purpose

The risk engine classifies every proposed agent action before it reaches approval.

## Risk Levels

```text
LOW
MEDIUM
HIGH
CRITICAL
BLOCKED
```

## Risk Signals

The engine scores requests using:

- project type
- file path
- change type
- branch target
- action type
- affected area
- test plan quality
- rollback plan quality
- secrets exposure risk
- runtime impact

## Default File Risk Rules

### LOW

- README updates
- documentation additions
- comments
- example files

### MEDIUM

- dashboard UI
- non-critical API routes
- config defaults
- test additions

### HIGH

- authentication
- authorization
- database migrations
- CI/CD workflows
- deployment config

### CRITICAL

- trading execution
- broker/order placement
- payment flows
- production infrastructure
- security policy enforcement

### BLOCKED

- `.env`
- credentials files
- private keys
- tokens
- secrets
- direct `main` or `master` branch edits

## Approval Intelligence Score

Elite version score dimensions:

- Code Risk
- Business Value
- Test Coverage
- Reversibility
- Evidence Strength

## MVP Rule

When in doubt, classify upward. False safety is worse than friction.
