# Approval Engine and Connector Contract

## Purpose and scope

The approval engine authorizes one exact action from one identified agent invocation. A phone decision is valid only for the request whose immutable payload the reviewer saw. It must not authorize a later or changed action.

This contract applies to supported local connectors, including Antigravity and the Codex local `PermissionRequest` hook. Each provider adapter must preserve its native invocation and return the provider's explicit allow or deny response. An API request, MCP request, simulator, or UI demonstration alone does not prove that a native provider session resumes.

The computer running the agent and connector must remain powered, connected, and authenticated. Cloud delivery cannot resume an offline, sleeping, terminated, or otherwise unavailable local process.

## Identity and immutable request

### Connector protocol version

The Antigravity and Codex local approval adapters send
`X-Aixion-Approval-Protocol: 1` on every authenticated approval operation:
create, read decision, consume, report an ambiguous delivery failure, and
expire. The API currently implements version `1`. An explicit unsupported
version is rejected with HTTP `426 Upgrade Required` and the response header
`X-Aixion-Supported-Approval-Protocols: 1`; no approval operation runs. For
backward compatibility, a missing version header is interpreted as legacy v1.
New connectors must send the header. A future incompatible change must add a
new version and retain v1 only while its compatibility and security guarantees
can still be met.

A connector-created request is bound to:

- account and workspace;
- project;
- provider;
- authenticated connector installation (`source_agent_id`);
- provider session (`source_session_id`), when supplied by the provider;
- one invocation (`source_invocation_id`);
- exact action type, tool name, target, and arguments or content needed to identify the action;
- risk assessment and displayed context;
- creation time, expiry time, and request ID.

The authenticated connector identity is derived from the server-side credential and must not be accepted from client-supplied identity fields. The invocation ID is created by the adapter from provider-documented invocation identifiers and exact action input. If required identity is absent, the connector denies before creating or approving an action.

The server stores a frozen payload hash for the request. Approval binds to that hash. Any changed request identity, action, target, arguments, risk snapshot, or hash is a different request and cannot reuse the decision.

A connector retry may resolve to the existing request only when workspace, connector installation, invocation ID, and payload/risk identity all match. Reuse of the same invocation identity with a different payload is a conflict and must fail closed.

## State model

### Server-authoritative request state

The current request lifecycle uses these canonical states:

```text
REQUESTED -> APPROVED
REQUESTED -> DENIED
REQUESTED -> EXPIRED
REQUESTED -> CANCELLED
APPROVED -> CANCELLED
```

A blocked request is not actionable and cannot be approved through the normal decision path. Legacy status names are mapped to canonical names by the application. Later execution states such as `EXECUTING`, `FAILED`, `READY_FOR_PR`, and `MERGED` belong to separate governed execution flows; they do not mean that a local tool permission was delivered.

Terminal request states are `DENIED`, `EXPIRED`, and `CANCELLED`. They cannot transition back to `REQUESTED` or `APPROVED`. A new provider invocation requires a new request identity.

### Connector delivery state

Delivery is separate from the reviewer decision:

```text
NOT_CLAIMABLE
    request is not approved, is expired/cancelled, identity does not match,
    or the approved payload hash is invalid

CLAIMABLE
    request is approved and its connector, workspace, invocation, and
    approved payload hash match; no prior claim exists

CONSUMED
    the owning connector atomically inserted the unique delivery claim
    and its audit event

DELIVERY_FAILED
    the connector could not establish a valid claim or could not safely
    return the claim result to the same waiting invocation
```

`CONSUMED` is a durable, single-use claim, not proof that the provider executed the action successfully. `DELIVERY_FAILED` describes a connector-observed failure. The server records one narrow `CLAIM_RESPONSE_AMBIGUOUS` report after the matching claim exists, tied to the workspace, connector, invocation, and payload hash. It writes an idempotent failure row and audit event but does not alter the reviewer decision or claim. Reporting is best-effort and never changes deny into allow. It does not prove whether the host received or executed the action, and it is not a general delivery-failure terminal state.

A request in `APPROVED` state is not permission to proceed by itself. The connector must claim the decision before returning the provider's allow response. Only the same pending invocation may receive that response.

## Decision and delivery rules

- Only an authenticated reviewer authorized for the request workspace may decide it.
- Decisions must use the durable request version and current status. The first valid terminal decision wins; racing or stale decisions fail without overwriting it.
- Approval records the approved payload hash. The connector verifies that hash before delivery.
- Rejection, expiry, cancellation, blocked status, mismatched identity/hash, missing connector capability, revoked credentials, and API errors all deny the local action.
- The owning connector must present its authenticated installation ID and exact invocation ID to claim delivery.
- The claim and `approval.delivery_claimed` audit record are committed atomically. A unique durable constraint permits at most one claim per approval and invocation identity.
- A duplicate claim returns conflict and must not return allow again.
- If claim insertion commits but the response is lost, the connector cannot distinguish that case from a claim failure. It denies this invocation; a new provider invocation and new approval may be required. It must not retry the old claim into another allow.
- The connector may report `CLAIM_RESPONSE_AMBIGUOUS` with the dedicated `REPORT_APPROVAL_DELIVERY_FAILURE` capability. The report is accepted only for the matching committed claim, workspace, connector, invocation, and payload hash; failure to report leaves the invocation denied.
- A connector must not return allow before a successful claim response has been validated.
- Provider timeout, connector restart, network loss, or backend restart cannot turn an unknown result into allow. On restart, the adapter may recreate/resolve the same request only through the stable idempotency identity, then re-read and claim; an already-consumed claim denies.
- Approval expiry is enforced by the API and checked by the connector. Late approval responses after the connector deadline are denied and are not claimed.
- The action is executed only by the provider's original waiting invocation. Control Tower does not replay or independently execute the action.

## Failure and retry matrix

| Condition | Required behavior |
| --- | --- |
| Missing workspace, connector, session/invocation identity, or action payload | Deny before request creation |
| Same invocation and same payload retried | Return/resolve the same request; do not duplicate audit or notification records |
| Same invocation with changed payload or risk | Conflict; deny |
| Reviewer decision races or arrives after terminal state | Preserve the durable winner; reject the stale decision |
| Request expires before decision or delivery | Expire or deny; never claim |
| Connector credential revoked or capability absent | Deny; never claim |
| API timeout or disconnect before a validated claim response | Deny the local action |
| Claim committed but response lost | Treat as ambiguous; deny old invocation; do not issue a second allow |
| Connector restarts while request is pending | Resolve by stable request identity and re-read durable state |
| Duplicate phone tap or duplicate claim | Idempotent decision handling where supported; claim remains at most once |
| Workspace, connector, invocation, action, or payload hash mismatch | Deny and emit a redacted security diagnostic |
| Provider process is gone or asleep | No resumption is promised; preserve request state and fail closed |

## Required approval payload for governed work

Every governed work request should contain:

- project and work-order IDs;
- title and summary;
- action type and exact target;
- affected files and diff or patch summary when applicable;
- risk level and explanation;
- test plan and rollback plan;
- provider, agent name, and invocation identity.

Tool-permission adapters must show the exact provider action that will run, not a generic work-order summary. Secrets must not be sent to the phone or written to diagnostics unless explicitly required for the decision; secret-like or oversized inputs should be denied by the adapter.

## Blocking rules

Approval must be blocked when:

- the target branch is `main` or `master` under the applicable project policy;
- a path appears to contain secrets or credentials;
- a file-change diff is missing;
- critical-risk work lacks a test plan or rollback instructions;
- the action has no clear purpose;
- required workspace, connector, session, or invocation identity is absent;
- request or approved payload identity cannot be verified.

## Audit and diagnostics

Decision and delivery audit records must bind actor, request ID, workspace, previous and new status, decision/reason, time, risk snapshot, payload hash, connector installation, and invocation ID where applicable.

The successful delivery claim is audited as `approval.delivery_claimed` with `delivery=at_most_once`. A reported ambiguous claim response is audited as `approval.delivery_failed` with only the fixed reason code and scoped identifiers; raw transport errors and tool arguments are excluded. Logs and metrics should expose correlation IDs and terminal outcomes, but must redact credentials and avoid unnecessary action contents. Even a durable report leaves provider execution outcome unknown.

## Implementation evidence and current limitations

The current code contains durable request states, payload-hash approval binding, invocation idempotency, expiry, credential/capability checks, and SQLite/PostgreSQL at-most-once delivery claims. Antigravity and Codex adapters deny on hook/API failures and validate claim responses before returning allow.

The following are not established by repository tests alone:

- general durable `DELIVERY_FAILED` terminal-state handling or proof of provider execution outcome; only the narrow ambiguous-claim report is implemented;
- a real Codex app permission round trip through Android;
- all Antigravity network-loss, restart, sleep/wake, and revocation cases;
- PostgreSQL as the authoritative routed application store;
- hosted delivery, background/locked FCM wake-up, or off-LAN behavior.

These limitations remain open exit gates. A local test, mock API, simulator, or repository-level test is not equivalent to a real provider-session approval round trip.

## Required decision audit fields

Every decision audit event must include:

- actor;
- decision and reason;
- timestamp;
- request ID and workspace;
- previous and new status;
- approved request payload hash and relevant risk snapshot.
