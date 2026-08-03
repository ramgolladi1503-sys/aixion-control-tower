# Aixion Agent Trust Control Plane

## Product boundary

Aixion is an independent governance and execution layer between human operators and AI agents.

```text
Agent intent
-> normalized ProposedAction
-> signed capability lease
-> deterministic trust policy
-> human exception decision when required
-> durable AgentRun step
-> immutable consumption evidence
-> tamper-evident flight recorder
```

It does not grant arbitrary shell, secret, database, deployment, direct-main, or auto-merge access.

## Capability lease

A lease is issued only after an exact `ApprovalRequest` payload is approved and sealed. It binds:

```text
approval payload hash
agent/provider identity
repository
feature branch
allowed action types
approved files
approved validation commands
network domains
runtime budget
cost budget
retry budget
pull-request budget
expiry
reviewer requirements
```

The receipt is signed with `AIXION_LEASE_SIGNING_KEY`. Production startup fails when the signing key or trust enforcement is missing.

### Modes

- `STRICT`: every meaningful action becomes a mobile exception requiring a signed decision for that exact immutable payload.
- `BOUNDED`: actions inside the signed lease pass automatically.
- `SUPERVISED`: ordinary in-scope actions pass; high-risk action families require a human decision.

V1 supports repository workflow capabilities only:

```text
READ_REPOSITORY
CREATE_BRANCH
MODIFY_FILES
RUN_COMMAND
ACCESS_NETWORK
CREATE_PULL_REQUEST
```

V1 deliberately rejects deployment, secret access, database writes, custom capability classes, direct protected-branch mutation, and auto-merge.

## Universal agent gateway

External agents authenticate through the existing agent credential boundary and submit a normalized `ProposedAction`:

```http
POST /trust/gateway/actions
```

The policy returns:

```text
ALLOW
REQUIRE_APPROVAL
BLOCK
```

A policy `BLOCK` cannot be overridden. The agent or operator must revise the approval scope and create a new action.

A `REQUIRE_APPROVAL` decision can be resolved only for the exact action:

```http
POST /trust/gateway/actions/{action_id}/decision
```

The signed decision is immutable and becomes the latest effective policy decision.

## Durable-run enforcement

Production enables:

```text
AIXION_TRUST_ENFORCEMENT=true
```

Before `CREATE_BRANCH`, `APPLY_CHANGES`, `RUN_VALIDATION`, or `CREATE_PULL_REQUEST`, the run supervisor:

1. validates the attached capability lease;
2. reconstructs the exact next action;
3. evaluates policy;
4. blocks or waits for human approval when required;
5. executes the existing fail-closed worker path;
6. records actual runtime, retries, output references, and evidence hash against the lease.

A run cannot be rebound to another capability lease after attachment.

## Short-lived credentials

The internal credential broker issues random capability tokens whose plaintext is returned once and whose hash alone is stored. Tokens are bounded by:

```text
lease status
lease expiry
grant expiry
audience
subject
scope
revocation
```

The introspection endpoint verifies all boundaries:

```http
POST /trust/credentials/introspect
```

Revoking a capability lease also revokes its child credentials.

GitHub App installation-token and OIDC federation issuance intentionally return `501 Not Implemented` until a deployment-specific issuer and trust configuration are installed. Aixion never fabricates external credentials.

## Flight recorder

Every trust event is chained to the previous event using canonical SHA-256 material:

```text
sequence
previous hash
event type
entity
actor
correlation id
payload
timestamp
```

Verification endpoint:

```http
GET /trust/flight-recorder/verify
```

Payload modification, sequence gaps, and predecessor mismatch are detected.

## Mobile exception console

The Android Runs destination is exception-first. It shows:

```text
blocked actions
actions needing exact approval
runs needing human intervention
failed runs
agent scope-adherence metrics
evidence-completion metrics
recovery and first-attempt rates
```

The phone remains a decision console rather than a miniature terminal.

## Evidence-backed reliability

Scorecards are computed from stored execution truth:

```text
policy decisions
scope adherence
first-attempt completion
stale-lease recovery
human intervention
policy blocks
sealed run evidence
runtime
recorded cost
```

They are not produced by an LLM judge.

## Observability

The existing Mission Control Prometheus endpoint now includes trust metrics:

```text
aixion_capability_leases_total
aixion_policy_decisions_total
aixion_policy_decisions_by_provider_total
aixion_action_authorizations_total
aixion_action_consumptions_total
aixion_credential_grants_active
aixion_credential_grants_revoked_total
aixion_trust_flight_recorder_valid
aixion_trust_flight_recorder_events_total
```

## API summary

```text
POST   /trust/attestations
GET    /trust/attestations
POST   /trust/leases
GET    /trust/leases
GET    /trust/leases/{lease_id}
POST   /trust/leases/{lease_id}/revoke
POST   /trust/gateway/actions
POST   /trust/gateway/actions/manual
POST   /trust/gateway/actions/{action_id}/decision
GET    /trust/gateway/actions/{action_id}/decision
POST   /trust/gateway/actions/{action_id}/consume
POST   /trust/gateway/actions/{action_id}/consume/manual
POST   /trust/credentials
POST   /trust/credentials/introspect
POST   /trust/credentials/{grant_id}/revoke
GET    /trust/exceptions
GET    /trust/scorecards
GET    /trust/flight-recorder
GET    /trust/flight-recorder/verify
POST   /agent/runs/{run_id}/capability-lease
```

## Honest deployment status

This release is designed as a production-hardened **single-node control-plane candidate**. It does not claim:

```text
multi-region high availability
horizontal multi-writer SQLite execution
arbitrary untrusted-code sandboxing
real GitHub App/OIDC token brokering before issuer integration
unattended production deployment
auto-merge
```

Move to PostgreSQL, a transactional outbox, and a durable distributed workflow runtime only after real concurrency and availability requirements justify that migration.
