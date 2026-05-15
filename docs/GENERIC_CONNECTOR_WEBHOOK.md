# Generic Connector Webhook

This document defines the PR 107 generic inbound webhook connector.

## Purpose

The connector registry and credential governance layers make it possible to configure external agents. PR 107 makes the first usable ingress path so OpenClaw, Antigravity-style bridges, Gemini/Claude/Cursor-style tools, and custom agents can create scoped AgentTasks or append task events.

## Endpoint

```http
POST /connectors/{connector_id}/webhook
```

## Supported actions

```text
CREATE_AGENT_TASK
APPEND_AGENT_TASK_EVENT
```

Flexible schema mapping is deliberately not included in this PR. PR 108 should add configurable JSON field mapping.

## Authentication

Bearer/API key connectors send:

```http
Authorization: Bearer <connector-secret>
```

HMAC connectors send:

```http
X-Aixion-Connector-Signature: <hex-hmac>
```

The current HMAC contract signs the raw request body using the stored connector secret hash as the key. This is MVP-grade and should be revisited before public production exposure.

## Create task payload

```json
{
  "action": "CREATE_AGENT_TASK",
  "project_id": "project_xxx",
  "title": "Fix Android approval flow",
  "goal": "Create a scoped task from a connector webhook.",
  "context": "Submitted by OpenClaw bridge.",
  "requested_action": "GENERATE_DOCS",
  "repository": "ramgolladi1503-sys/aixion-control-tower",
  "branch_preference": "feature/webhook-task",
  "requires_approval": true,
  "metadata": {
    "source": "openclaw"
  }
}
```

## Append event payload

```json
{
  "action": "APPEND_AGENT_TASK_EVENT",
  "task_id": "agent_task_xxx",
  "event_type": "PLAN_RECEIVED",
  "message": "Connector produced a plan.",
  "status": "PLANNING",
  "metadata": {
    "artifact": "plan"
  }
}
```

## Enforcement

The webhook enforces:

```text
connector enabled status
configured secret
bearer or HMAC auth
allowed actions
allowed project scopes
allowed repository scopes
owned-task event append
basic per-minute rate limit
metadata size cap
audit events for accepted/refused/rate-limited calls
```

## Audit events

```text
connector.webhook_task_created
connector.webhook_task_event_appended
connector.webhook_refused
connector.webhook_rate_limited
```

## Remaining gaps

```text
no configurable schema mapper yet
no connector templates yet
no Android connector UI yet
no public deployment hardening yet
HMAC contract should be strengthened before public production use
```
