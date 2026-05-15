# Connector Schema Mapper

This document defines PR 108: configurable JSON schema mapping for connector webhooks.

## Purpose

PR 107 made connectors usable, but only for agents that send Aixion's exact webhook shape. That is not enough for bring-your-own-agent support.

PR 108 lets owners configure how a foreign agent payload maps into Aixion's normalized webhook payload before task creation or event append.

## Owner APIs

Get mapper status:

```http
GET /connectors/{connector_id}/schema-mapper
```

Set mapper:

```http
PUT /connectors/{connector_id}/schema-mapper
```

Preview mapper output:

```http
POST /connectors/{connector_id}/schema-mapper/preview
```

## Mapper config

```json
{
  "enabled": true,
  "default_action": "CREATE_AGENT_TASK",
  "field_paths": {
    "title": "task.title",
    "goal": "task.objective",
    "context": "task.notes",
    "repository": "repo.full_name",
    "branch_preference": "repo.branch",
    "metadata": "extra"
  },
  "defaults": {
    "project_id": "project_xxx",
    "requested_action": "GENERATE_DOCS",
    "requires_approval": true
  }
}
```

## Supported target fields

```text
action
project_id
title
goal
context
source_url
source_session_id
source_task_id
requested_action
repository
branch_preference
risk_hint
requires_approval
metadata
task_id
event_type
message
status
```

## Path rules

Paths use simple dot notation:

```text
task.title
repo.full_name
event.status
```

Unsafe path parts are rejected.

## Webhook behavior

When a connector has an enabled mapper, webhook flow becomes:

```text
raw external payload
-> schema mapper normalization
-> existing ConnectorWebhookPayload validation
-> scope checks
-> AgentTask creation or AgentTaskEvent append
```

If no mapper is configured, the webhook keeps accepting Aixion-native payloads.

## Preview behavior

The preview endpoint returns the normalized payload without creating a task or event. This is the safe way to test OpenClaw, Antigravity, Gemini, Claude/Cursor, or custom local bridge payloads before turning them loose.

## Scope boundary

This PR does not add connector templates or Android UI. Templates belong in PR 109. Android management belongs in PR 110.
