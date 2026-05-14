# Agent Worker Validation Command Runner

This document describes the first controlled validation command runner for approved AgentTasks.

## Purpose

The validation runner executes only allowlisted validation commands from the linked ApprovalRequest `test_plan` and records results in the AgentTask timeline.

This is the first real command execution step in the worker path, so it is deliberately narrow.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
loads the linked ApprovalRequest
validates test_plan using the validation planner rules
runs commands without shell=True
uses shlex parsing
uses per-command timeout
captures stdout/stderr summary
stops on first failure
writes TESTS_STARTED event
writes TESTS_PASSED or TESTS_FAILED event
writes audit events
releases the worker lease
persists state
```

## What it does not do

```text
no GitHub API calls
no branch creation
no file writes
no commits
no pull request creation
no approval decision changes
no arbitrary shell execution
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_validation_runner.py --task-id agent_task_xxx --cwd ..
```

Run against first approved task:

```bash
python scripts/run_agent_worker_validation_runner.py --first-approved --cwd ..
```

Set command timeout:

```bash
python scripts/run_agent_worker_validation_runner.py --task-id agent_task_xxx --timeout-seconds 120
```

Print JSON:

```bash
python scripts/run_agent_worker_validation_runner.py --task-id agent_task_xxx --json
```

## Safety rules

The runner reuses the validation planner rules:

```text
allowlisted command prefixes only
no shell=True
no shell control tokens
no curl/wget/ssh/scp/sudo/chmod/chown/mkfs
max command length enforced
max command count enforced
```

## Timeline events

Start:

```text
TESTS_STARTED
status=TESTING
commands_executed=false
```

Success:

```text
TESTS_PASSED
status=APPROVED
commands_executed=true
results=[command/exit_code/timed_out/output_summary]
```

Failure:

```text
TESTS_FAILED
status=FAILED
commands_executed=true
results=[command/exit_code/timed_out/output_summary]
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_validation_runner.py
python -m pytest
```

Tests inject fake executors, so CI verifies runner state transitions and evidence without depending on real project command execution.

## Hard truth

This is real command execution, but not full worker delivery. It validates the approved test plan and records pass/fail evidence. It still does not create branches, write files, commit, or open pull requests.
