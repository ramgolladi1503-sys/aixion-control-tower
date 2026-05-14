# GPT Actions Contract

This folder documents the first safe Custom GPT / GPT Actions contract for Aixion Control Tower.

## Why this exists

PR #78 created the agent task inbox. PR #79 bridged agent tasks into the approval engine. This contract tells ChatGPT or a Custom GPT how to call that doorway without exposing dangerous owner controls.

The goal is narrow on purpose:

```text
External agent -> create task -> check task -> append progress event -> operator reviews in Aixion
```

This is not worker execution yet. It is the contract layer that allows a GPT to submit and track work safely.

## Files

```text
docs/gpt-actions/openapi.yaml
docs/PUBLIC_HTTPS_CALLBACK_GUIDE.md
```

Use the OpenAPI file as the Custom GPT action schema. Use the public HTTPS guide before trying to connect ChatGPT, Android, workers, or MCP clients to the backend.

## Exposed operations

```text
GET  /projects
POST /agent/tasks
GET  /agent/tasks/{task_id}
POST /agent/tasks/{task_id}/events
```

These are intentionally safe external-agent operations:

- project discovery
- task creation
- task status lookup
- timeline/progress reporting

## Deliberately not exposed

Do not expose these through the GPT Actions contract:

```text
owner role management
invite/session revocation
demo seed/reset routes
raw approval decision routes
GitHub execution runner routes
MCP registry/admin routes
ops/recovery routes
unsafe destructive routes
```

Hard truth: if a Custom GPT can directly approve, reset, execute, or mutate admin state, the mobile control tower becomes security theater. The GPT should submit work and report evidence. The human/operator control plane should decide.

## Authentication

The backend expects:

```http
Authorization: Bearer <AIXION_SESSION_TOKEN>
```

For a demo, generate a token through `/auth/login` and configure the Custom GPT action authentication to send it as a bearer token.

For production, do not reuse a personal owner session forever. Create a scoped external-agent credential model or a dedicated maintainer/reviewer account with limited access and rotation.

## Public HTTPS requirement

Custom GPT Actions cannot reliably call a laptop-only `localhost` backend.

Before testing GPT Actions, follow:

```text
docs/PUBLIC_HTTPS_CALLBACK_GUIDE.md
```

Minimum requirement:

```text
public HTTPS backend URL
valid bearer token
server URL in openapi.yaml updated to the public backend
GET /projects works from the GPT Action test console
```

## Custom GPT setup

1. Deploy Aixion backend to an HTTPS URL or expose a short-lived demo tunnel.
2. Confirm auth is enabled outside intentional demo mode.
3. Open the Custom GPT builder.
4. Add an Action.
5. Import `docs/gpt-actions/openapi.yaml`.
6. Replace the server URL:

```yaml
servers:
  - url: https://YOUR_AIXION_BACKEND_HOST
```

7. Configure authentication so the action sends:

```http
Authorization: Bearer <token>
```

8. Test with `GET /projects` first.
9. Test `POST /agent/tasks` with a low-risk documentation task.
10. Confirm the task appears in Aixion and that timeline events are recorded.

## Recommended GPT instruction

Use wording like this inside the Custom GPT instructions:

```text
You are an external agent connected to Aixion Control Tower.
When the user asks you to start implementation, create an Aixion agent task first.
Do not claim work is approved until Aixion shows APPROVED.
Do not call unsafe admin, reset, approval-decision, or execution endpoints.
Record meaningful progress events when planning, blocked, failed, ready for PR, or done.
Always include repository, branch preference, risk hint, and source session context when available.
```

## Example task payload

```json
{
  "provider": "CHATGPT",
  "project_id": "project_tradebot",
  "title": "Prepare release notes for approval bridge",
  "goal": "Summarize PR #79 and remaining connected-agent gaps.",
  "context": "Include what changed, validation evidence, and what is still not connected.",
  "source_session_id": "chatgpt-session-2026-05-15",
  "requested_action": "GENERATE_DOCS",
  "repository": "ramgolladi1503-sys/aixion-control-tower",
  "branch_preference": "feature/release-notes",
  "risk_hint": "LOW",
  "requires_approval": true,
  "metadata": {
    "source": "custom-gpt-action"
  }
}
```

## Example event payload

```json
{
  "event_type": "PLAN_RECEIVED",
  "message": "Plan drafted. Waiting for operator approval before implementation.",
  "status": "PLANNING",
  "metadata": {
    "evidence_url": "https://github.com/ramgolladi1503-sys/aixion-control-tower/pull/80"
  }
}
```

## Validation checklist

Before claiming this contract is ready:

```text
OpenAPI imports into the GPT action builder
GET /projects succeeds with bearer auth
POST /agent/tasks creates a task
GET /agent/tasks/{task_id} returns that task
POST /agent/tasks/{task_id}/events appends a timeline event
No owner/admin/reset/execute endpoints are exposed
Server URL points to HTTPS, not localhost
```

## Current limitation

This does not make ChatGPT execute code or open PRs. That belongs to the Codex/GitHub worker contract. This contract only gives GPT Actions a safe doorway into the existing connected-agent task and approval flow.
