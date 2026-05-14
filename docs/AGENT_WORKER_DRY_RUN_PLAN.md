# Agent Worker Dry-Run Plan

This plan defines the safest first implementation after the worker contract.

## Why dry-run first

A worker that immediately writes files, pushes branches, and opens pull requests can damage trust if the contract is wrong. The first implementation should prove the lifecycle without repository mutation.

## Dry-run worker objective

Build a small script or service that:

```text
finds one approved AgentTask
validates required fields
writes EXECUTION_STARTED
writes RESULT_READY
never changes repository files
never pushes branches
never opens a pull request
```

This proves that the worker can safely observe approval state and report back to the Android timeline.

## Candidate command

```bash
cd backend
python scripts/run_agent_worker_dry_run.py --task-id agent_task_xxx
```

Optional automatic selection:

```bash
python scripts/run_agent_worker_dry_run.py --first-approved
```

## Required behavior

The dry-run worker should:

```text
load backend store/API context
find the requested approved task
verify status == APPROVED
verify approval_request_id exists
verify task is not DENIED, FAILED, CANCELLED, or DONE
append EXECUTION_STARTED event with status EXECUTING
append RESULT_READY event with status DONE
include dry_run=true in metadata
persist state
```

## Refusal behavior

The dry-run worker should refuse and write or print a clear failure when:

```text
task not found
task not approved
task missing approval_request_id
task already terminal
backend/auth unavailable
```

For dry-run mode, do not mark the task FAILED just because it was not eligible. Eligibility failure is not execution failure.

## Suggested tests

```text
approved linked task gets EXECUTION_STARTED and RESULT_READY events
non-approved task is refused
missing approval_request_id is refused
terminal task is refused
metadata includes dry_run=true and worker_id
```

## Out of scope

```text
branch creation
file patching
GitHub API calls
pull request creation
real command execution
retry leases
claim locks
multi-worker concurrency
```

## Expansion path

After dry-run proves the lifecycle:

```text
PR +1: add claim/lease semantics
PR +2: add safe branch creation
PR +3: add patch application
PR +4: add validation command runner
PR +5: add PR opening and result URLs
```

Hard truth: if dry-run is skipped, we will debug worker behavior and approval lifecycle at the same time. That is sloppy. Prove the loop first, then mutate repos.
