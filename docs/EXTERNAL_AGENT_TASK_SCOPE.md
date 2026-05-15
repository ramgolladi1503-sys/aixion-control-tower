# Scoped External AgentTask Access

This document describes the first scoped external-agent access model for AgentTasks.

## Purpose

External agents such as ChatGPT, Custom GPT Actions, Codex-like workers, Claude, Cursor, and GitHub Actions should not need normal owner/maintainer user sessions to create and update AgentTasks.

Aixion already has external-agent registration, hashed agent tokens, and allowed project/repository/action scopes. This layer extends that model to AgentTasks.

## What it adds

```text
external-agent AgentTask creation
external-agent owned-task read
external-agent owned-task event listing
external-agent owned-task event append
project scope enforcement
repository scope enforcement
action scope enforcement
ownership enforcement
```

## New AgentAction values

```text
CREATE_AGENT_TASK
APPEND_AGENT_TASK_EVENT
READ_AGENT_TASK
```

Existing values remain:

```text
CREATE_APPROVAL
CREATE_WORK_ORDER
EXECUTE_GITHUB
```

## Endpoints

Create scoped AgentTask:

```http
POST /agents/tasks
```

Read owned AgentTask:

```http
GET /agents/tasks/{task_id}
```

List owned AgentTask events:

```http
GET /agents/tasks/{task_id}/events
```

Append owned AgentTask event:

```http
POST /agents/tasks/{task_id}/events
```

## Required headers

```http
X-Aixion-Agent-Id: agent_xxx
X-Aixion-Agent-Token: aixion_agent_xxx
```

The token is returned once during agent registration and stored only as a hash.

## Scope rules

The external agent must have:

```text
CREATE_AGENT_TASK to create tasks
READ_AGENT_TASK to read owned tasks/events
APPEND_AGENT_TASK_EVENT to append owned events
allowed_project_ids must include the task project unless empty wildcard scope is intentionally used
allowed_repositories must include the task repository unless empty wildcard scope is intentionally used
```

## Ownership rules

External agents can read and append events only for tasks where:

```text
task.external_agent_id == authenticated_agent.id
```

That prevents one external agent from reading or mutating another agent's task timeline.

## What it does not expose

```text
no approval decision route
no owner/admin routes
no session/user routes
no retry/cancel routes
no worker execution routes
no GitHub runner routes
no MCP admin routes
```

## Example registration

```json
{
  "provider": "CHATGPT",
  "display_name": "Custom GPT Builder",
  "auth_type": "API_KEY",
  "allowed_project_ids": ["project_xxx"],
  "allowed_repositories": ["ramgolladi1503-sys/aixion-control-tower"],
  "allowed_actions": [
    "CREATE_AGENT_TASK",
    "READ_AGENT_TASK",
    "APPEND_AGENT_TASK_EVENT"
  ],
  "enabled": true
}
```

## Example task creation

```json
{
  "project_id": "project_xxx",
  "title": "Prepare implementation plan",
  "goal": "Create a safe plan and wait for approval before implementation.",
  "context": "Submitted by a scoped Custom GPT action.",
  "requested_action": "GENERATE_DOCS",
  "repository": "ramgolladi1503-sys/aixion-control-tower",
  "branch_preference": "feature/scoped-agent-task",
  "requires_approval": true,
  "metadata": {
    "source": "custom-gpt-action"
  }
}
```

The backend sets:

```text
provider=<authenticated agent provider>
external_agent_id=<authenticated agent id>
external_agent_name=<authenticated agent display name>
metadata.external_agent_scoped=true
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_external_agent_task_scope.py
python -m pytest
```

## Hard truth

This improves external-agent safety, but it is not full production token governance. Token rotation, per-token expiry, per-endpoint rate limits, and revocation UX still need dedicated hardening.
