# Connected-Agent Worker Contract

This document defines the worker contract for Aixion Control Tower.

The backend now has agent task intake, an approval bridge, a GPT Actions contract, and an Android Agent Tasks screen. The next required layer is a clear contract for a Codex, GitHub Actions, or other build worker to continue only after human approval and report useful evidence back to Aixion.

## Goal

A worker must turn an approved AgentTask into traceable delivery evidence:

```text
AgentTask APPROVED
worker starts
worker records progress events
worker prepares changes on a safe branch
worker runs validation
worker reports PR/result/failure evidence
Android operator can see the timeline
```

## Non-goals

This contract does not add a production worker implementation yet.

It does not add:

```text
new backend endpoints
new Android screens
new deployment infrastructure
cloud runners
secret storage
automatic merge behavior
bypass of human approval
```

## Required inputs

A worker needs this minimum data from an AgentTask:

```text
task id
provider
repository
branch preference
goal
context
requested action
approval request id
status
risk hint
metadata
```

A worker must refuse to continue if:

```text
task status is not APPROVED
approval_request_id is missing
repository is missing for repository work
target branch is protected or unsafe
auth token is missing or invalid
task was cancelled, denied, failed, or already done
```

## Required lifecycle mapping

Workers must write task events through:

```text
POST /agent/tasks/{task_id}/events
```

Use these mappings:

```text
worker picked up task       -> EXECUTION_STARTED + EXECUTING
validation started          -> TESTS_STARTED + TESTING
validation passed           -> TESTS_PASSED + TESTING or READY_FOR_PR
validation failed           -> TESTS_FAILED + FAILED
pull request created        -> PR_CREATED + READY_FOR_PR
final non-PR result ready   -> RESULT_READY + DONE
unrecoverable error         -> FAILED + FAILED
cancelled externally        -> CANCELLED + CANCELLED
completed after evidence    -> DONE + DONE
```

## Event payload examples

### Start

```json
{
  "event_type": "EXECUTION_STARTED",
  "message": "Worker picked up approved task and started safe branch preparation.",
  "status": "EXECUTING",
  "metadata": {
    "worker_id": "github-actions-worker-1",
    "repository": "ramgolladi1503-sys/aixion-control-tower",
    "branch": "feature/pr83-example"
  }
}
```

### Validation passed

```json
{
  "event_type": "TESTS_PASSED",
  "message": "Validation passed for the prepared change set.",
  "status": "TESTING",
  "metadata": {
    "commands": ["python -m pytest", "./gradlew testDebugUnitTest"],
    "summary": "Backend and Android JVM tests passed."
  }
}
```

### Pull request created

```json
{
  "event_type": "PR_CREATED",
  "message": "Pull request opened with validation evidence.",
  "status": "READY_FOR_PR",
  "metadata": {
    "pr_url": "https://github.com/org/repo/pull/123",
    "branch": "feature/approved-task-123",
    "commit_sha": "abc123"
  }
}
```

### Failure

```json
{
  "event_type": "FAILED",
  "message": "Worker failed before opening a pull request.",
  "status": "FAILED",
  "metadata": {
    "error_code": "VALIDATION_FAILED",
    "failed_command": "./gradlew testDebugUnitTest",
    "summary": "Kotlin unit-test compilation failed."
  }
}
```

## Branch safety rules

A worker must:

```text
create or reuse only a feature branch
never push directly to main/master
never force-push protected branches
never merge its own PR
include task id in branch name when possible
record branch and commit SHA in task events
```

Recommended branch pattern:

```text
feature/agent-task-{short_task_id}-{slug}
```

## PR evidence requirements

Every worker-created PR should include:

```text
linked AgentTask id
linked ApprovalRequest id
summary of what changed
validation commands and results
rollback notes
known limitations
Aixion task timeline reference
```

## Failure reporting requirements

A failure is only useful if it is actionable. Workers must report:

```text
where it failed
why it failed
what command or step failed
whether retry is safe
what human action is needed
links to logs when available
```

Bad failure message:

```text
Build failed.
```

Good failure message:

```text
Android JVM tests failed during compileDebugUnitTestKotlin because a shared API interface was widened and existing fake APIs did not implement the new method. Retry is safe after isolating the new feature behind a dedicated API interface.
```

## Human-control policy

Workers must not become the approval authority.

Allowed:

```text
read approved task
write progress event
prepare branch
run validation
open PR
report evidence
report failure
```

Not allowed:

```text
approve own task
deny own task
merge own PR
change owner roles
revoke users
reset demo or production data
change production secrets
bypass risk policy
```

## Minimum demo loop

A demo-ready worker loop should prove this sequence:

```text
1. Agent task exists.
2. Linked approval exists.
3. Operator approves on Android.
4. Worker sees APPROVED task.
5. Worker records EXECUTION_STARTED.
6. Worker records TESTS_STARTED.
7. Worker records TESTS_PASSED or TESTS_FAILED.
8. Worker records PR_CREATED or FAILED.
9. Android Agent Tasks screen shows the timeline.
```

## Implementation recommendation

The next code PR should not try to build a giant autonomous worker. Start with a small dry-run worker:

```text
read one approved task
validate required fields
write EXECUTION_STARTED
do not mutate repository yet
write RESULT_READY with dry-run evidence
```

Then expand safely:

```text
branch creation
file patching
validation commands
PR opening
retry and failure handling
```

Hard truth: without this contract, a worker is just a risky script. With this contract, the worker becomes an auditable part of the mobile control tower.
