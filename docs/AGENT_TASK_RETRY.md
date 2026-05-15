# AgentTask Retry Reporting

This document describes the first retry reporting layer for failed AgentTask worker runs.

## Purpose

The worker orchestrator can now run an approved task through branch creation, file application, validation, and PR creation. When a worker run fails, the operator needs a clear and auditable way to decide whether the task can be retried.

This retry layer is deliberately event/audit driven. It does not add a new database schema field.

## What it does

```text
adds retry eligibility summary
adds operator retry endpoint
allows retry only from FAILED tasks
requires a linked approval request
counts previous retry requests from task events
limits retries with max_retries
clears stale worker lease fields
resets task status to APPROVED
writes retry-requested timeline event
writes audit evidence
```

## What it does not do

```text
no automatic retry loop
no worker execution by itself
no branch cleanup
no PR cleanup
no merge
no approval decision changes
```

## Endpoints

Check retry eligibility:

```http
GET /agent/tasks/{task_id}/retry
```

Request retry:

```http
POST /agent/tasks/{task_id}/retry
```

Example payload:

```json
{
  "reason": "Validation failed because of a transient CI issue.",
  "max_retries": 3
}
```

## Retry rules

Allowed only when:

```text
task.status == FAILED
task.approval_request_id exists
retry_count < max_retries
```

Rejected when:

```text
task is not FAILED
task has no linked approval
retry limit is reached
```

## Timeline event metadata

Retry creates a `NOTE` event with:

```text
operation_type=agent_task_retry_requested
retry_count=<new count>
previous_retry_count=<previous count>
max_retries=<limit>
reason=<operator reason>
worker_lease_cleared=true
```

## Status transition

```text
FAILED -> APPROVED
```

This lets the approved-task worker orchestrator claim the task again.

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_task_retry.py
python -m pytest
```

## Hard truth

This is retry control, not retry automation. The operator still has to trigger the worker again after retry is requested. That is intentional for now because automatic retry loops without cleanup and evidence are how agents create messes fast.
