# Agent Trust Control Plane — Production Readiness

## Release classification

```text
Target: production-hardened single-node release candidate
Not claimed: multi-node HA or unrestricted autonomous execution
Merge state: draft until every deterministic gate is green
Deployment state: not certified until a real operator campaign succeeds
```

## Mandatory repository gates

All must pass on the exact PR head:

```text
backend lint
full backend test suite
Android JVM tests
Android debug APK
Android release build
production Compose configuration validation
Mission Control API container health
stateless scheduler container health
/health
/ops/readiness
/agent/runs/summary
/agent/runs/metrics
trust policy negative controls
flight-recorder tamper tests
strict exact-action approval test
bounded action-consumption test
credential expiry and revocation tests
```

## Required production configuration

```text
AIXION_PROFILE=production
AIXION_AUTH_ENABLED=true
AIXION_TRUST_ENFORCEMENT=true
AIXION_DB_PATH=<persistent path>
AIXION_LEASE_SIGNING_KEY=<secret-manager value>
FCM_SERVER_KEY=<secret-manager value>
AIXION_OWNER_TOKEN=<short-lived owner session>
```

GitHub access requires one of:

```text
GITHUB_TOKEN
```

or the complete GitHub App set:

```text
AIXION_GITHUB_APP_ID
AIXION_GITHUB_APP_INSTALLATION_ID
AIXION_GITHUB_APP_PRIVATE_KEY
```

The GitHub App configuration passes startup readiness, but actual installation-token issuance remains deployment work until a real issuer adapter is certified.

## Required security checks

- Approval creator cannot attest the same request.
- A reviewer denial prevents lease issuance.
- Lease scope cannot exceed approved files or validation commands.
- Lease receipt signature must verify before action execution.
- Expired or revoked leases block all subsequent actions.
- Credentials cannot outlive the parent lease.
- Credential token hashes are never returned by APIs.
- Direct `main` or `master` mutation is rejected.
- Auto-merge is rejected.
- Policy `BLOCK` cannot be overridden by a reviewer.
- Strict-mode approval applies only to the exact immutable action payload.
- Actual usage is checked against runtime, cost, retry, and pull-request budgets.
- Every allowed side effect produces immutable consumption evidence.
- Trust-event chain verification must remain green.

## Required operational checks

- Persistent volume survives API restart.
- Recovery snapshot exports successfully.
- Scheduler talks only to the API and never mounts SQLite.
- API remains the single durable-store writer.
- Metrics scrape uses an authenticated maintainer token.
- Grafana anonymous access is disabled.
- Public TLS termination and rate limiting exist in front of the API.
- Backup and restore are exercised on a disposable environment.
- Mobile push delivery is verified on a real device.
- Signing-key rotation has an operator runbook.
- Token and session revocation are tested.

## Flagship deployment certification

Use a disposable repository and complete this exact campaign:

1. A configured external agent submits Agent Work.
2. A reviewer approves exact files, tests, branch, and rollback.
3. Aixion issues a signed 15-minute capability lease.
4. The lease is attached to exactly one durable AgentRun.
5. A strict action is surfaced on Android and approved for its exact payload.
6. A controlled worker failure causes `RETRY_WAIT`, not success.
7. The stale lease is recovered without duplicate branch or file mutation.
8. A forbidden-file action is permanently blocked.
9. A validation failure prevents PR creation.
10. A corrected bounded retry passes validation.
11. Exactly one draft PR is created.
12. `main` remains unchanged and auto-merge remains disabled.
13. Runtime, retry, PR, and evidence consumption match the lease.
14. The flight-recorder chain verifies.
15. Android, audit records, GitHub state, Prometheus, and the final evidence hash agree.

## Blocking limitations

The following prevent a claim of general enterprise production readiness:

```text
SQLite single-node persistence
no certified external GitHub App/OIDC issuer
no multi-tenant isolation certification
no external penetration test
no sustained load/chaos certification
no multi-region disaster recovery
```

These do not prevent a controlled single-team pilot. They must remain visible in release notes and sales material.

## Safe release statement

After repository gates and the flagship campaign pass:

> Aixion is a production-hardened single-node agent trust control plane for controlled pilot use, with signed capability leases, policy-enforced durable execution, exact human exceptions, short-lived internal credentials, evidence-backed reliability, and tamper-evident audit history.

Do not state that the platform is universally production-ready for arbitrary untrusted agents or regulated multi-tenant deployment until the blocking limitations are closed.
