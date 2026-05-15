# Connector Registry Foundation

This document defines the PR 105 backend foundation for configurable add-on agents.

## Purpose

Aixion must support bring-your-own-agent connectors instead of hardcoding support for one agent platform. Users should be able to register tools such as OpenClaw, Antigravity, Gemini-based agents, Claude/Cursor-style agents, local bridges, and custom HTTP agents.

PR 105 creates the registry only. It does not ingest webhook payloads yet and it does not manage connector secrets yet.

## Connector types

```text
GENERIC_HTTP
WEBHOOK
MCP
LOCAL_BRIDGE
GPT_ACTIONS
CUSTOM
```

## Provider labels

```text
OPENCLAW
ANTIGRAVITY
GEMINI
CLAUDE
CURSOR
CHATGPT
CODEX
CUSTOM
```

## Auth types

```text
NONE
API_KEY
BEARER
HMAC
```

## Owner APIs

```http
POST /connectors
GET /connectors
GET /connectors/{connector_id}
PATCH /connectors/{connector_id}
POST /connectors/{connector_id}/disable
POST /connectors/{connector_id}/enable
```

All connector registry APIs are owner-only.

## Registry fields

Each connector stores:

```text
name
connector_type
provider_label
endpoint_url
callback_url
auth_type
status
health_status
allowed_project_ids
allowed_repositories
allowed_actions
rate_limit_per_minute
secret_ref
last_used_at
last_health_check_at
failed_auth_count
last_error
config
created_at
updated_at
```

## Safety boundaries

The registry validates project scopes and URL shape. Public endpoints must use HTTPS. Local development may use localhost or 127.0.0.1 HTTP.

The public response exposes `secret_configured` only. It does not expose raw secrets or secret references.

## What is deliberately not included

```text
no connector secret creation/rotation
no inbound webhook endpoint
no schema mapper
no test connection
no Android connector screen
```

Those belong in later PRs so each risk boundary stays reviewable.

## Example OpenClaw-style connector

```json
{
  "name": "OpenClaw Local Bridge",
  "connector_type": "LOCAL_BRIDGE",
  "provider_label": "OPENCLAW",
  "endpoint_url": "http://localhost:8787",
  "callback_url": "https://example.com/connectors/callback",
  "auth_type": "HMAC",
  "allowed_project_ids": ["project_xxx"],
  "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
  "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
  "rate_limit_per_minute": 30,
  "enabled": true,
  "config": {
    "profile": "openclaw",
    "mode": "approval-gated"
  }
}
```

## Next PR

PR 106 should add connector credential and health governance: secret references, secret rotation/revocation, last-used tracking, failed-auth counters, and health status updates.
