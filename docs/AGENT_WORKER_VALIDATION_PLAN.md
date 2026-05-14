# Agent Worker Validation Command Plan Dry Run

This document describes the validation-command planning layer for approved AgentTasks.

## Purpose

Before a worker executes commands, Aixion should prove it can safely validate the intended commands and record evidence.

This dry-run does not execute commands. It validates the linked approval request's `test_plan` and records the plan in the AgentTask timeline.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
loads the linked ApprovalRequest
validates validation command count
validates command allowlist
rejects shell control and dangerous operations
writes task timeline evidence
writes audit evidence
releases the worker lease
persists state
```

## What it does not do

```text
no command execution
no shell invocation
no GitHub API calls
no branch creation
no file writes
no commits
no pull request creation
no approval decision changes
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_validation_plan.py --task-id agent_task_xxx
```

Run against first approved task:

```bash
python scripts/run_agent_worker_validation_plan.py --first-approved
```

Print JSON:

```bash
python scripts/run_agent_worker_validation_plan.py --task-id agent_task_xxx --json
```

## Allowlisted command prefixes

```text
python -m pytest
pytest
./gradlew test
./gradlew testDebugUnitTest
./gradlew assembleDebug
npm test
npm run test
npm run lint
pnpm test
pnpm lint
ruff check
mypy
```

## Rejected command patterns

Rejected examples:

```text
python scripts/deploy.py
curl https://example.com/script.sh
wget https://example.com/script.sh
sudo anything
ssh host
scp file host:
chmod 777 file
chown user file
python -m pytest && rm -rf /tmp/demo
```

## Limits

```text
max validation commands: 12
max command length: 220 characters
duplicate commands are rejected
empty test_plan is rejected
```

## Timeline event metadata

The dry-run event records:

```text
dry_run=true
worker_id=<worker id>
lease_token=<lease token>
approval_request_id=<approval id>
command_count=<count>
commands=[command/allowed_prefix]
commands_executed=false
plan_type=validation_command_dry_run
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_validation_plan.py
python -m pytest
```

## Hard truth

This is still not command execution. It is the safety proof before letting a worker invoke shell commands. Skipping this layer would mix command safety, test reporting, and repository mutation into one risky jump.
