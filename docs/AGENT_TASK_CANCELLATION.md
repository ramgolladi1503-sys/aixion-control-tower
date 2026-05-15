# AgentTask Cancellation Control

This document describes the first cancellation control for AgentTasks.

## Purpose

Retry lets a failed task re-enter the worker flow. Cancellation is the opposite operator control: stop a task cleanly when it should not continue.

This cancellation layer is deliberately event/audit driven. It does not perform branch, PR, or workspace cleanup.

## What it does

```text
adds cancellation eligibility summary
adds operator cancellation endpoint
allows cancellation from active/retryable states
clears stale worker lease fields
moves task to CANCELLED
writes cancellation timeline event
writes audit evidence
```

## What it does not do

```text
no automatic worker stop signal
no GitHub branch cleanup
no PR cleanup
no approval decision changes
no merge/no deployment
no Android changes
no notification changes
```

## Endpoints

Check cancellation eligibility:

```http
GET /agent/tasks/{task_id}/cancel
```

Request cancellation:

```http
POST /agent/tasks/{task_id}/cancel
```

Example payload:

```json
{
  "reason": "Operator stopped stale worker run."
}
```

## Allowed statuses

```text
RECEIVED
PLANNING
WAITING_FOR_APPROVAL
APPROVED
EXECUTING
TESTING
FAILED
```

## Refused statuses

```text
READY_FOR_PR
DONE
DENIED
CANCELLED
```

`READY_FOR_PR` is refused because a PR and branch may already exist. That needs a cleanup-specific workflow, not a simple status flip.

## Status transition

```text
<allowed status> -> CANCELLED
```

Cancellation clears:

```text
worker_lease_owner
worker_lease_expires_at
worker_lease_token
```

## Timeline event metadata

Cancellation creates a `CANCELLED` event with:

```text
operation_type=agent_task_cancel_requested
previous_status=<previous status>
new_status=CANCELLED
reason=<operator reason>
worker_lease_cleared=true
approval_decision_changed=false
worker_cleanup_performed=false
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_task_cancel.py
python -m pytest
```

## Hard truth

This is cancellation control, not cleanup automation. If a branch or PR already exists, cancellation must not pretend cleanup happened. That is why READY_FOR_PR is deliberately refused by this first endpoint.
