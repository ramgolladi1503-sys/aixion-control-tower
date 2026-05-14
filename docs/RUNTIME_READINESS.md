# Runtime Readiness Endpoint

Aixion Control Tower now has a small runtime readiness endpoint for operators.

This is not monitoring, alerting, deployment automation, or a full health stack. It is a diagnostic endpoint that answers one practical question:

```text
Can this backend instance prove its core runtime dependencies are usable right now?
```

## Endpoint

```text
GET /ops/readiness
```

Access:

```text
OWNER
MAINTAINER
```

Reviewers cannot access it.

## What it reports

The response reports:

```text
status
profile
auth_enabled
db_reachable
migrations_applied
expected_migration_ids
applied_migration_ids
recovery_snapshot_available
recovery_format_version
github_token_configured
fcm_server_key_configured
errors
warnings
generated_at
```

Secret values are never returned. GitHub and FCM configuration are reported as booleans only.

## Example response

```json
{
  "status": "ready",
  "generated_at": "2026-05-14T00:00:00Z",
  "profile": "local",
  "auth_enabled": false,
  "db_reachable": true,
  "migrations_applied": true,
  "expected_migration_ids": ["0001_baseline_kv_store"],
  "applied_migration_ids": ["0001_baseline_kv_store"],
  "recovery_snapshot_available": true,
  "recovery_format_version": "aixion-control-tower-recovery-v1",
  "github_token_configured": false,
  "fcm_server_key_configured": false,
  "errors": [],
  "warnings": [
    "GitHub token is not configured; GitHub execution will not be available.",
    "FCM server key is not configured; push notifications will not be available."
  ]
}
```

## Readiness rules

The endpoint returns `status: ready` only when:

```text
DB migration metadata can be read
all known backend migrations are applied
recovery snapshot export can be assembled
startup environment validation has no errors
```

Missing GitHub or FCM configuration creates warnings in local/demo/test profiles. In production, the startup environment validation already prevents unsafe boot when those required secrets are missing.

## Curl

```bash
curl -H "Authorization: Bearer <owner-or-maintainer-token>" \
  http://localhost:8000/ops/readiness
```

## Why this exists

Before this endpoint, the backend could expose `/health`, but `/health` only proved the HTTP process answered.

That is too weak for operation-grade behavior.

This readiness endpoint proves more useful runtime facts:

```text
SQLite is reachable
migration metadata is readable
expected migrations are present
recovery snapshot contract is available
profile/auth mode is visible
GitHub/FCM integration configuration is visible without leaking secrets
```

## What this does not solve yet

This does not add:

```text
Kubernetes readiness probes
Prometheus metrics
centralized logging
uptime monitoring
alert routing
synthetic transaction checks
scheduled recovery drills
production deployment automation
```

Hard truth: `/ops/readiness` is an operator diagnostic layer, not production observability. Real production still needs metrics, logs, traces, alerts, restore drills, and deployment health checks.
