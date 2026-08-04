# Native Agent Control Tower Product Contract

## Status

Authoritative product contract for Aixion's connected-agent direction.

This document supersedes any interpretation that makes an Aixion-owned replacement runtime the primary customer experience.

## Customer promise

A customer installs Aixion once, signs in, connects supported desktop agents once, and continues working in those agents' native applications.

When a connected native agent requests a human decision, Aixion must show that exact request on the customer's approved mobile device. A decision made in Aixion must be delivered back to the same native process, session, thread, turn, and provider request. The native agent then continues from that decision.

The customer must not need to:

- copy the task into another Aixion-owned agent window;
- relaunch the task through a terminal;
- start a duplicate hidden agent session;
- approve the same action in two applications;
- abandon the provider's native desktop application.

## Canonical flow

```text
Native desktop agent is already running
→ provider emits a structured approval request
→ supported native connector observes the request
→ Aixion binds it to the exact provider/session/turn/request identity
→ Aixion backend publishes one canonical approval action
→ approved Aixion mobile device displays the exact action
→ user approves or rejects
→ first valid decision wins atomically
→ native connector resolves the original provider request
→ same native process/session/thread/turn continues
→ result and audit evidence return to Aixion
```

## Required identity continuity

A successful native integration must preserve and prove:

- provider identity;
- host/device identity;
- provider process identity when exposed;
- native session/thread identity;
- native turn identity;
- provider request identity;
- provider-supported decisions;
- exact command, file change, tool call, or permission amendment;
- working directory and execution scope;
- canonical payload hash;
- one decision and one provider response;
- continuation of the same native execution context.

A connector that starts a new provider process or new hidden thread does not satisfy this contract.

## Product modes

Aixion supports two explicitly different modes.

### 1. Native existing-session control

The customer continues using the provider's native desktop application. Aixion attaches through a documented and supported provider interface.

Customer label:

```text
Native session
```

This is the preferred product mode.

### 2. Aixion-managed agent session

Aixion launches and owns a provider runtime through a supported SDK, CLI, app-server, or worker interface.

Customer label:

```text
Aixion-managed session
```

This is a valid fallback and automation mode, but it must never be described as control of an already-running native desktop session.

## Native connector certification gate

A provider may be shown as supporting native-session control only when all of the following are proven through a documented provider contract:

1. Discover or identify an existing native session.
2. Attach without replacing the provider's owning client.
3. Stream the live native session state.
4. Observe structured approval requests.
5. Resolve the exact original approval request.
6. Preserve the provider's available decisions without translation drift.
7. Continue the same native execution context.
8. Revoke the connection and device authorization.
9. Avoid credential extraction, UI scraping, private-protocol impersonation, or binary patching.
10. Pass real-device approve and reject certification.

Missing any required capability means native control is not available.

## Prohibited production shortcuts

The following cannot be used as the production native connector:

- OCR or screenshot parsing;
- macOS Accessibility automation that clicks provider buttons;
- terminal or log scraping;
- private websocket or relay impersonation;
- extraction or reuse of provider account tokens;
- TLS interception;
- binary patching or injection;
- undocumented attachment to a provider-owned process;
- a separately launched duplicate session presented as the existing native session.

These methods are too fragile, unsafe, and unsupported for a commercial control plane.

## Codex-specific current boundary

OpenAI provides first-party remote continuity for supported Codex desktop sessions through the ChatGPT mobile application's Remote experience. OpenAI describes live session state, approvals, terminal output, diffs, tests, and other context flowing through its secure relay.

As of this contract's creation, the reviewed public OpenAI materials do not document a third-party enrollment, subscription, or approval-resolution API for an external application to control an already-running native Codex desktop session.

Therefore:

```text
Codex native existing-session control: PARTNER_OR_PUBLIC_API_REQUIRED
Codex Aixion-managed app-server session: AVAILABLE_AS_EXPLICIT_FALLBACK
```

The managed app-server bridge must not be used as evidence that native Codex integration is complete.

Official references:

- https://openai.com/index/work-with-codex-from-anywhere/
- https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex
- https://help.openai.com/en/articles/20001256-plugins-in-codex/

## Multi-provider product vision

Aixion's defensible product is not merely a second mobile client for one coding agent.

Aixion is a vendor-neutral approval, supervision, and governance plane across supported providers, including:

- native desktop agents where a supported attach interface exists;
- Aixion-managed agent sessions;
- CI and GitHub workers;
- MCP tools and gateways;
- custom enterprise agents;
- approval escalation and delegated operators;
- organization policy and auditable decisions.

Each provider connector must declare exactly what it can do. The UI must not collapse managed and native modes into a generic `CONNECTED` badge.

## Required customer setup

The target onboarding is:

```text
Install Aixion Desktop
→ sign in
→ pair approved phone
→ choose provider
→ complete provider-supported authorization
→ Aixion verifies connector capabilities
→ customer keeps using the provider's native app
```

If the provider does not expose a supported native interface, the setup must say so and offer only accurately labeled alternatives.

## Product-state language

Allowed:

- `Native connector available`
- `Native connector requires provider access`
- `Aixion-managed session available`
- `Approval channel connected`
- `Backend connected; approval channel not verified`

Disallowed:

- `Connected` when only backend health is known;
- `Native Codex connected` when Aixion launched another Codex runtime;
- `Certified` without a physical mobile approve and reject test;
- `Every command requires approval` when the provider automatically classifies safe read-only actions.

## Current honest status

```text
CONTROL_PLANE_FOUNDATION_IMPLEMENTED
AIXION_MANAGED_CODEX_BRIDGE_IMPLEMENTED_NOT_CERTIFIED
NATIVE_CODEX_THIRD_PARTY_CONNECTOR_REQUIRES_SUPPORTED_PROVIDER_INTERFACE
PRODUCT_VISION_LOCKED_TO_NATIVE_EXISTING_SESSION_CONTROL
```
