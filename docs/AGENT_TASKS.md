# Agent Task Ingestion API

Aixion now has the first backend doorway for connected-agent work.

This is the foundation for ChatGPT, Codex, Claude, Cursor, GitHub Actions workers, and manual operators to submit tracked work into the control tower.

## Endpoints

```text
POST /agent/tasks
GET /agent/tasks
GET /agent/tasks/{task_id}
GET /agent/tasks/{task_id}/events
POST /agent/tasks/{task_id}/events
```

## Current scope

This PR adds:

```text
AgentTask model
AgentTaskEvent model
provider/status/action enums
task creation
task listing/filtering
task detail lookup
task event timeline append
audit events
persistence
tests
```

## Current auth model

This first slice uses the existing user/session role gates:

```text
maintainer: create tasks and append events
reviewer: list/read tasks and events
```

External agent token ingestion, GPT Actions, and provider-specific worker execution are deliberately left for later PRs.

## Example task

```json
{
  "provider": "CHATGPT",
  "project_id": "project_demo_aixion_control",
  "title": "Prepare RTI app MVP scope",
  "goal": "Create product scope, backend plan, Android flow, risks, and demo checklist.",
  "requested_action": "GENERATE_DOCS",
  "requires_approval": true
}
```

## Event example

```json
{
  "event_type": "TESTS_STARTED",
  "message": "Running backend tests",
  "status": "TESTING"
}
```

## What this does not do yet

```text
automatic approval creation
GPT Actions/OpenAPI contract
Codex/GitHub worker execution
Android Agent Tasks screen
FCM task notification lifecycle
public callback/deployment path
```

Hard truth: this PR gives agents a real task inbox. It does not yet make the workers autonomous.
