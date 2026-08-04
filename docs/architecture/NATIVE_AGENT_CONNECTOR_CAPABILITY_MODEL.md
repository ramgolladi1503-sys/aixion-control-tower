# Native Agent Connector Capability Model

## Purpose

Aixion must distinguish connection to a backend from control of a provider's existing native session.

A generic `CONNECTED` state is not sufficient. Every adapter must declare how it owns or attaches to the provider runtime and which approval capabilities are proven.

## Connector modes

### `NATIVE_EXISTING_SESSION`

Aixion attaches to a session already owned by the provider's native desktop client.

Required product behavior:

- no duplicate provider process;
- no duplicate hidden thread;
- provider desktop remains the owning client;
- exact native approval request is observable;
- phone decision resolves the original native request;
- the same execution context continues.

### `AIXION_MANAGED_SESSION`

Aixion starts and owns a provider runtime through a documented SDK, CLI, app-server, worker, or protocol.

This mode may offer the same mobile approval UX, but it is not attachment to a native desktop session.

## Required native capabilities

A native connector is eligible only when every required capability is publicly supported and evidence-backed:

```text
ATTACH_EXISTING_SESSION
STREAM_SESSION_STATE
OBSERVE_APPROVAL_REQUESTS
RESOLVE_APPROVAL_REQUESTS
PRESERVE_PROVIDER_DECISIONS
CONTINUE_SAME_EXECUTION_CONTEXT
REVOKE_CONNECTION
```

Optional capabilities include:

```text
DISCOVER_EXISTING_SESSIONS
STEER_SESSION
CANCEL_SESSION
```

Managed-runtime capabilities include:

```text
START_MANAGED_RUNTIME
STRUCTURED_PROVIDER_EVENTS
```

## Support states

```text
PUBLIC_SUPPORTED
PARTNER_REQUIRED
FIRST_PARTY_ONLY
MANAGED_ONLY
UNVERIFIED
UNSUPPORTED
```

Only `PUBLIC_SUPPORTED` may pass the public native-connector gate.

`PARTNER_REQUIRED` means the product architecture is valid but the provider must grant a supported integration surface before shipment.

`FIRST_PARTY_ONLY` means the provider offers the capability to its own clients but does not document third-party access.

`MANAGED_ONLY` means Aixion can own a separate runtime but cannot attach to the native application.

## Fail-closed eligibility

The backend function `evaluate_native_control()` must return `native_control_eligible=false` unless:

- mode is `NATIVE_EXISTING_SESSION`;
- support is `PUBLIC_SUPPORTED`;
- every required capability exists;
- a public provider contract is recorded;
- at least one evidence reference is recorded.

The UI and registration APIs must consume this result rather than inferring support from provider name, process presence, backend health, or adapter naming.

## Connection-state model

Aixion should expose distinct states:

```text
BACKEND_REACHABLE
HOST_REGISTERED
PROVIDER_AUTHORIZED
NATIVE_SESSION_DISCOVERED
NATIVE_APPROVAL_CHANNEL_VERIFIED
MOBILE_DEVICE_SUBSCRIBED
END_TO_END_APPROVAL_CERTIFIED
```

A green backend health check may set only `BACKEND_REACHABLE`.

The customer-facing `Native connector available` state requires the native approval channel to be verified.

## Approval identity contract

The canonical action must bind:

```text
provider
host_id
connector_id
native_session_id
native_thread_id
native_turn_id
provider_request_id
provider_item_id
available_decisions
command_or_change_payload
cwd
sandbox_scope
network_scope
permission_amendment
payload_hash
expiry
```

The resolution must include the canonical action ID and payload hash. First valid resolution wins atomically. Duplicate or mismatched resolutions fail closed.

## Provider inventory

The code-level provider inventory must be conservative.

For Codex:

```text
codex-native-desktop
mode: NATIVE_EXISTING_SESSION
support: PARTNER_REQUIRED
native eligible: false
```

```text
codex-aixion-managed-app-server
mode: AIXION_MANAGED_SESSION
support: MANAGED_ONLY
native eligible: false
customer label: Aixion-managed session
```

## UI requirements

The Connectors screen must display:

- provider;
- connector mode;
- support state;
- proven capabilities;
- missing capabilities;
- evidence or provider-access requirement;
- last verification time;
- exact reason a native connector is unavailable.

The UI must not show a native provider as fully connected merely because:

- the backend is healthy;
- an account is logged in;
- an Aixion-managed runtime can start;
- an adapter process is installed;
- a first-party mobile client can perform remote control.

## Migration rule

Existing relay adapters that use broad flags such as `NATIVE_APPROVALS` must migrate to the explicit capability contract.

Until migration completes, broad legacy flags must not make a connector native-eligible.
