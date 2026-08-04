# Codex Native Approval Connector Feasibility

## Principal verdict

```text
OPENAI_PARTNER_OR_PUBLIC_API_REQUIRED
```

## Question

Can Aixion observe and resolve approval requests belonging to an already-running native Codex desktop session through a documented and supported third-party interface?

## Confirmed first-party capability

OpenAI publicly documents remote continuity for supported Codex desktop sessions through the ChatGPT mobile application.

The official product material describes:

- live state from the host environment;
- active threads;
- approvals;
- screenshots;
- terminal output;
- diffs and test results;
- remote steering and continuation;
- a secure relay that keeps the host reachable without directly exposing it to the public internet.

This confirms that native-session remote approval continuity exists inside OpenAI's first-party product.

Official references:

- https://openai.com/index/work-with-codex-from-anywhere/
- https://help.openai.com/en/articles/6825453-chatgpt-release-notes
- https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex

## Public third-party surface reviewed

### Codex mobile Remote

Documented as a ChatGPT mobile experience connected to Codex on a host machine.

No reviewed public document describes external mobile-client enrollment, an approval subscription API, or an approval-resolution API for third-party products.

### Codex plugins

Plugins package skills and approved app-backed capabilities used by Codex to reach external systems.

The reviewed plugin documentation does not state that a plugin may subscribe to or resolve Codex's own shell/file-change approval lifecycle.

Reference:

- https://help.openai.com/en/articles/20001256-plugins-in-codex/

### Access tokens

OpenAI documents access tokens for trusted, non-interactive automation in eligible workspaces.

That use case does not establish a public contract for attaching an external interactive approval client to an already-running native Codex desktop turn.

### Codex app-server

The existing Aixion research proves that Aixion can start and own a Codex app-server runtime and answer its structured approval requests.

That is an `AIXION_MANAGED_SESSION`. It does not prove attachment to the native Codex desktop application's already-running process or turn.

## Why the current Desktop Bridge is not the target connector

The current bridge starts a separate provider runtime, creates its own thread, and owns the approval request from startup.

It can potentially provide:

- structured events;
- exact provider decisions;
- mobile approval routing;
- same-process continuation for the runtime it owns.

It cannot honestly claim:

- attachment to the user's existing native Codex desktop session;
- observation of approvals created by the native Codex UI;
- resolution of the native UI's original provider request;
- continuity of the user's pre-existing native thread.

Therefore it remains a fallback and automation mode.

## Unsupported shortcuts rejected

The following were rejected as production architecture:

- macOS Accessibility clicks;
- OCR or screenshot parsing;
- terminal/log scraping;
- private relay impersonation;
- token extraction;
- TLS interception;
- binary patching;
- undocumented attachment to provider-owned IPC;
- hidden duplicate sessions presented as native control.

## Required OpenAI capability

Aixion needs an official third-party native-session integration contract containing at least:

1. OAuth or workspace-admin authorization for Aixion.
2. Trusted host discovery and device pairing.
3. Existing Codex session/thread discovery.
4. A scoped subscription to structured session events.
5. Structured approval-request events.
6. Exact provider decision schema.
7. An atomic endpoint or channel to resolve the original request.
8. Same-session continuation acknowledgement.
9. Revocation, expiration, and device management.
10. Enterprise audit and data-retention controls.
11. Clear commercial and redistribution terms.

## Product decision

### Now

- Preserve the app-server bridge as `Aixion-managed Codex session`.
- Do not merge it as proof of native Codex control.
- Enforce the native-versus-managed capability model.
- Pursue OpenAI partner or public API access.
- Continue building vendor-neutral control-plane capabilities for providers and systems that expose supported integration surfaces.

### Native Codex release gate

Do not ship `Native Codex` until all of the following pass:

```text
SUPPORTED_PROVIDER_CONTRACT
EXISTING_NATIVE_SESSION_DISCOVERED
STRUCTURED_APPROVAL_OBSERVED
PHYSICAL_ANDROID_APPROVE_PASS
PHYSICAL_ANDROID_REJECT_PASS
SAME_NATIVE_PROCESS_THREAD_TURN_CONTINUES
ATOMIC_SINGLE_RESOLUTION_PASS
REVOCATION_AND_RECONNECT_PASS
```

## Current product status

```text
AIXION_CONTROL_PLANE_FOUNDATION_IMPLEMENTED
AIXION_MANAGED_CODEX_SESSION_IMPLEMENTED_NOT_CERTIFIED
NATIVE_CODEX_CONNECTOR_BLOCKED_ON_SUPPORTED_PROVIDER_INTERFACE
```
