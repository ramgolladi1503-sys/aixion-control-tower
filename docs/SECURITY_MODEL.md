# Security Model

## Security Principle

Aixion Control Tower must never become a remote button for unsafe AI actions.

The product exists to make AI work safer, not faster at any cost.

## Non-Negotiable Controls

- No direct main branch edits.
- No secret or credential edits from mobile.
- No silent file changes.
- No high-risk approval without visible diff.
- No critical approval without tests and rollback plan.
- No production deployment approval in MVP.
- Every action is audited.

## Sensitive File Patterns

Requests touching these paths must be blocked by default:

```text
.env
.env.*
*secret*
*credential*
*token*
*.pem
*.key
id_rsa
credentials.py
secrets.yaml
```

## Branch Safety

Agents must work on feature branches only.

Blocked branches:

```text
main
master
production
release
```

## Mobile Approval Safety

High-risk approvals must require explicit review inside the app. Notification-only approval is not allowed for high-risk or critical requests.

## Audit Requirements

Audit events must preserve:

- actor
- request id
- project id
- decision
- timestamp
- risk level
- reason
- branch
- affected files
- test result

## MVP Security Mode

Default mode is Strict Mode.

Everything meaningful requires manual review until the workflow proves reliable.
