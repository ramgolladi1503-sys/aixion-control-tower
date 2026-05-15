# Connector Credential and Health Governance

This document defines the PR 106 credential and health governance layer for configurable agent connectors.

## Purpose

The connector registry lets owners configure add-on agents such as OpenClaw, Antigravity, Gemini-style agents, Claude/Cursor-style agents, local bridges, and custom HTTP agents.

A configurable connector is dangerous unless its credential lifecycle and health state are controlled. PR 106 adds that lifecycle without adding public webhook ingestion yet.

## Owner APIs

Inspect credential status:

```http
GET /connectors/{connector_id}/credentials
```

Issue first secret:

```http
POST /connectors/{connector_id}/secret/issue
```

Rotate secret:

```http
POST /connectors/{connector_id}/secret/rotate
```

Revoke secret:

```http
POST /connectors/{connector_id}/secret/revoke
```

Mark health success/failure manually or from future connector checks:

```http
POST /connectors/{connector_id}/health/success
POST /connectors/{connector_id}/health/failure
```

## Secret behavior

Secrets are returned once from issue/rotate endpoints.

Public connector responses expose only:

```text
secret_configured=true|false
```

They do not expose:

```text
raw secret
secret hash
secret hint
secret ref
```

## Health behavior

The connector stores:

```text
last_used_at
last_health_check_at
failed_auth_count
last_error
health_status
```

Health states:

```text
UNKNOWN
HEALTHY
DEGRADED
FAILING
DISABLED
```

## What this PR does not add

```text
no inbound webhook ingestion
no connector authentication middleware
no schema mapper
no test-connection call to remote endpoint
no Android UI
```

Those are separate PRs because each is a separate risk boundary.

## Next PR

PR 107 should add the generic inbound webhook connector:

```text
POST /connectors/{connector_id}/webhook
bearer/HMAC validation
payload normalization into AgentTask
scope enforcement
rate limiting
AgentTaskEvent append support
```
