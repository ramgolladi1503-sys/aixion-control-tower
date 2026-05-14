# Agent Worker Dry-Run Lifecycle

This document describes the first backend worker implementation for connected-agent tasks.

## Purpose

The dry-run worker proves the approved-task worker lifecycle without touching repositories.

It writes lifecycle evidence into the AgentTask timeline so Android can show that an approved task was picked up and completed by a worker.

## What it does

```text
finds an approved AgentTask
checks it has a linked approval_request_id
checks worker claim lease availability
claims the task with worker_lease_owner / worker_lease_token / worker_lease_expires_at
writes EXECUTION_STARTED with status EXECUTING
writes RESULT_READY with status DONE
releases the worker lease after success
writes audit events
persists state
```

## What it deliberately does not do

```text
no repository mutation
no branch creation
no file patching
no command execution
no pull request creation
no automatic merge
no approval decision changes
```

## Command

Run by explicit task ID:

```bash
cd backend
python scripts/run_agent_worker_dry_run.py --task-id agent_task_xxx
```

Run against the first approved task with a linked approval:

```bash
cd backend
python scripts/run_agent_worker_dry_run.py --first-approved
```

Set lease duration:

```bash
python scripts/run_agent_worker_dry_run.py --task-id agent_task_xxx --lease-seconds 300
```

Print JSON:

```bash
python scripts/run_agent_worker_dry_run.py --task-id agent_task_xxx --json
```

## Eligibility rules

The dry-run worker refuses when:

```text
no task selector is provided
task does not exist
task status is not APPROVED
task is DENIED, FAILED, CANCELLED, or DONE
task is APPROVED but missing approval_request_id
task has an active worker lease owned by another worker
```

Eligibility refusal does not mark the task failed. That is intentional. A task that was never eligible for worker execution is not a worker failure.

## Claim lease behavior

The dry-run worker now uses a basic claim lease before writing execution events.

Lease fields live on AgentTask:

```text
worker_lease_owner
worker_lease_token
worker_lease_expires_at
```

Rules:

```text
task with no lease can be claimed
task with expired lease can be claimed by a new worker
task with active lease is refused
first-approved selection skips actively leased tasks
successful dry-run releases the lease
```

This is not full multi-worker concurrency control yet. It is the first guardrail that prevents two workers from casually processing the same approved task.

## Success timeline

A successful dry-run writes two task events:

```text
EXECUTION_STARTED -> status EXECUTING
RESULT_READY      -> status DONE
```

Both events include metadata:

```text
dry_run=true
worker_id=<worker id>
lease_token=<lease token>
```

The final result event also records:

```text
repository_mutated=false
branch_created=false
pull_request_opened=false
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_dry_run.py
python -m pytest
```

## Why this matters

This proves the worker reporting loop before the product starts changing files or opening pull requests. That sequencing matters. If worker lifecycle, approval status, Android timeline reporting, and claim semantics are not proven first, repository mutation will make failures harder to reason about.

Hard truth: this is not autonomous delivery yet. It is the smallest honest proof that approved work can move through a claimed worker reporting loop and appear as evidence in Aixion.
