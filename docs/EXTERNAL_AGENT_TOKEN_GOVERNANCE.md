# External Agent Token Governance

This document defines the PR #101 token-governance hardening layer for scoped external agents.

## Purpose

Scoped external AgentTask access is useful only if the credential lifecycle is controlled. A token that cannot be rotated, revoked, expired, monitored, or rate-limited is not production-safe.

This layer hardens external-agent credentials without widening what external agents are allowed to do.

## What it adds

```text
token rotation
token revocation
optional token expiry
last-used tracking
failed-auth audit events
failed-auth counters
basic per-agent rate limiting
owner-visible credential status
```

## Owner endpoints

Inspect credential state:

```http
GET /agents/{agent_id}/credentials
```

Rotate a token:

```http
POST /agents/{agent_id}/token/rotate
```

Optional rotation body:

```json
{
  "token_expires_at": "2026-06-15T00:00:00Z",
  "rate_limit_per_minute": 60
}
```

Revoke a token:

```http
POST /agents/{agent_id}/token/revoke
```

## Credential states

```text
ACTIVE          token exists, agent enabled, not expired, not revoked
DISABLED        agent exists but is disabled
EXPIRED         token expiry timestamp is in the past
MANUAL          manual agent; token authentication is not allowed
NOT_CONFIGURED  token-backed agent has no stored token hash
REVOKED         token was explicitly revoked
```

## Successful authentication behavior

On successful external-agent authentication, the backend updates:

```text
last_used_at
rate_limit_window_started_at
rate_limit_request_count
updated_at
```

No raw token is stored. Only the hash is stored.

## Failed authentication behavior

Failed external-agent authentication creates an audit event:

```text
agent.auth_failed
```

The audit details include only safe metadata:

```json
{
  "agent_id": "agent_xxx",
  "reason": "invalid_token"
}
```

The backend does not store or echo the supplied token.

Tracked failure reasons:

```text
missing_credentials
unknown_agent
agent_disabled
manual_agent_token_auth
token_revoked_or_missing
token_expired
invalid_token
```

## Rate-limit behavior

When an authenticated external agent exceeds its per-minute limit, the backend returns:

```http
429 External agent rate limit exceeded
```

The backend also records:

```text
agent.rate_limited
```

This is intentionally basic. It prevents a misconfigured or runaway connected agent from hammering AgentTask routes. It is not a distributed production limiter yet.

## Security boundaries

This PR does not change the external-agent permission model.

External agents still cannot access:

```text
owner/admin routes
approval decision routes
retry/cancel routes
worker execution routes
GitHub runner routes
MCP admin routes
normal user/session routes
another agent's task timeline
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_external_agent_token_governance.py
python -m pytest tests/test_external_agent_task_scope.py
python -m pytest
```

## Remaining production gap

This is real hardening, but it is not the final production credential system. The next level should move rate limiting out of the in-process store, add per-token identity/versioning, expose Android owner controls for rotation/revocation, and add alerting for repeated auth failures.
