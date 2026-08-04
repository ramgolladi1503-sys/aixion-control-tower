# OpenAI Codex Native Approval Integration Request

## Product

Aixion Control Tower is a vendor-neutral mobile approval, supervision, and governance plane for AI-assisted software work.

## Requested integration

Aixion requests a documented and supported interface that allows an authorized third-party control plane to observe and resolve approval requests belonging to an already-running Codex desktop session without replacing the Codex desktop client or starting a duplicate hidden session.

## Intended customer flow

```text
Customer signs in to Codex Desktop
→ customer signs in to Aixion
→ customer authorizes Aixion once
→ customer keeps working in Codex Desktop
→ Codex requests approval
→ exact approval appears in Aixion Android
→ customer approves or rejects
→ same Codex process/thread/turn receives the decision
→ Codex continues
```

## Minimum required capabilities

### Authorization and enrollment

- OAuth 2.0 / OIDC or workspace-admin authorization.
- Explicit scopes for session visibility and approval resolution.
- Trusted host and mobile-device enrollment.
- Revocation, expiration, device rotation, and user offboarding.
- Enterprise policy and role enforcement.

### Host and session discovery

- List authorized connected Codex hosts.
- List active sessions/threads on a selected host.
- Stable identifiers for host, process where available, session/thread, and turn.
- Ability to attach as an approved secondary control client without taking ownership away from Codex Desktop.

### Event subscription

A supported websocket, SSE, webhook, or equivalent channel carrying structured events for:

- session lifecycle;
- assistant messages and questions;
- command execution requests;
- file-change requests;
- permission/sandbox/network amendments;
- test and terminal results;
- completion, failure, and cancellation.

### Approval request schema

Each approval request should provide:

- stable provider request ID;
- host ID;
- session/thread ID;
- turn ID;
- item ID when available;
- exact command, file change, or tool request;
- working directory;
- reason;
- sandbox and network scope;
- requested policy amendment;
- provider-supported decisions;
- expiry;
- integrity hash or signature.

### Approval resolution

A supported operation that:

- accepts the original provider request ID;
- accepts only provider-supported decisions;
- binds the decision to the exact request payload or integrity hash;
- is atomic and idempotent;
- returns already-resolved state when another authorized client won the race;
- confirms whether the same Codex session and turn continued;
- emits an auditable resolution event.

### Security boundaries

- No exposure of ChatGPT session cookies or internal bearer tokens.
- No requirement to intercept private OpenAI relay traffic.
- Tenant, workspace, user, host, and device isolation.
- Short-lived scoped credentials.
- Signed events or verifiable request integrity.
- Rate limits and abuse controls.
- Explicit data-retention and telemetry rules.

### Commercial and policy requirements

- Permission to offer the integration in a third-party commercial product.
- Branding and naming requirements.
- Workspace-admin controls for Business, Enterprise, and Edu.
- Security review expectations.
- Support and versioning policy.
- Deprecation guarantees or migration notice.

## Aixion commitments

Aixion will:

- preserve exact provider decisions;
- fail closed on identity or payload mismatch;
- use first-valid-decision-wins semantics;
- display provider, host, session, turn, command, scope, and risk context;
- keep customer credentials on the host/provider side where required;
- support revocation and device separation;
- retain auditable approval evidence;
- avoid UI automation, OCR, private-protocol impersonation, token extraction, or binary patching.

## Pilot proposal

A limited partner pilot could certify:

1. One macOS Codex host.
2. One existing native Codex thread.
3. One physical Android device.
4. One harmless shell-command approval.
5. One file-change approval.
6. Approve and reject paths.
7. Same-process/thread/turn continuity.
8. Duplicate-resolution rejection.
9. Device revocation and reconnect.
10. Redacted audit export.

## Success criterion

```text
Aixion resolves the original approval request created by the already-running native Codex Desktop session, and that exact session continues without a duplicate provider runtime.
```
