# Connector Sample Event Simulator

This document defines PR 111: safe connector payload simulation.

## Purpose

The connector platform can now register external agents, issue secrets, receive webhooks, map foreign JSON, and manage connectors from Android. PR 111 adds a safe simulator so owners can test a sample payload before sending a real webhook.

## Backend API

```http
POST /connectors/{connector_id}/simulate
```

Example request:

```json
{
  "sample_payload": {
    "task": {
      "title": "Review generated code",
      "goal": "Prepare a safe implementation plan",
      "context": "OpenClaw local run"
    },
    "repo": {
      "full_name": "owner/repo",
      "branch": "feature/openclaw-task"
    },
    "metadata": {
      "source": "openclaw"
    }
  },
  "mapper": {
    "enabled": true,
    "default_action": "CREATE_AGENT_TASK",
    "field_paths": {
      "title": "task.title",
      "goal": "task.goal",
      "context": "task.context",
      "repository": "repo.full_name",
      "branch_preference": "repo.branch",
      "metadata": "metadata"
    },
    "defaults": {
      "project_id": "project_xxx",
      "requested_action": "GENERATE_DOCS",
      "requires_approval": true
    }
  }
}
```

## Simulator behavior

The simulator:

```text
normalizes payload through the connector schema mapper
validates the normalized payload against the live webhook payload model
checks whether the connector is enabled
checks whether a connector secret is configured
checks action scope
checks project and repository scope
builds a task preview for CREATE_AGENT_TASK
builds an event preview for APPEND_AGENT_TASK_EVENT
returns errors and warnings
```

The simulator does not:

```text
create AgentTasks
append AgentTaskEvents
consume webhook rate limits
mutate connector health
require a live webhook signature
call external agents
```

## Android behavior

The Android connector screen's preview action now calls the simulator endpoint. It shows whether the selected template payload is accepted, whether auth and scope are ready, plus errors, warnings, and the normalized payload.

## Why this matters

Without this simulator, owners would have to test connector setup by firing real webhooks. That is bad operational design. Simulation gives safe feedback before external agents are connected.

## Next PR

PR 112 should harden public callbacks:

```text
stronger HMAC contract
timestamp header
nonce/replay protection
signature versioning
public webhook deployment docs
```
