# Approved AgentTask Worker Orchestrator

This document describes the first orchestration command for running the approved AgentTask worker flow end to end.

## Purpose

The worker pieces now exist separately:

```text
branch creation
file application
validation command runner
pull request opening
```

The orchestrator runs those pieces in the correct order for one approved AgentTask.

## What it does

```text
starts orchestration audit event
creates a safe feature branch
applies approved file changes to that branch
runs allowlisted validation commands
opens a GitHub pull request if validation passes
writes a summary AgentTask event
writes completion/failure audit evidence
```

## Step order

```text
1. GitHub branch creation
2. GitHub file application
3. validation command runner
4. GitHub pull request creation
```

## Stop rules

The orchestrator stops immediately when a step fails:

```text
branch failure -> no files, no validation, no PR
file failure -> no validation, no PR
validation failure -> no PR
a PR is opened only after validation passes
```

## What it does not do

```text
no merge
no self-approval
no deployment
no approval decision changes
no Android changes
no notification preference changes
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_orchestrator.py --task-id agent_task_xxx --cwd ..
```

Use custom source/base branches:

```bash
python scripts/run_agent_worker_orchestrator.py \
  --task-id agent_task_xxx \
  --source-branch main \
  --base-branch main \
  --cwd ..
```

Print JSON:

```bash
python scripts/run_agent_worker_orchestrator.py --task-id agent_task_xxx --json
```

## Required environment

```text
GITHUB_TOKEN
```

The token must support the individual worker steps:

```text
read refs
create refs
read contents
write contents
open pull requests
```

## Timeline evidence

The orchestrator leaves the individual worker events plus a final summary event.

Successful summary:

```text
DONE
orchestration_success=true
branch=<branch>
files_written=<count>
files_deleted=<count>
commits_created=<count>
validation_commands=<count>
pull_request_url=<url>
pull_request_number=<number>
operation_type=approved_agent_task_orchestration
```

Failure summary:

```text
FAILED
orchestration_success=false
branch_success=<bool/null>
file_success=<bool/null>
validation_success=<bool/null>
pr_success=<bool/null>
operation_type=approved_agent_task_orchestration
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_orchestrator.py
python -m pytest
```

Tests use fake GitHub clients and fake validation execution. They do not call GitHub.

## Current limit

Validation runs in the configured working directory. It does not automatically checkout the newly created branch locally. For production-grade execution, a later runner should use an isolated workspace, checkout the branch, apply validation there, and upload logs/artifacts.

## Hard truth

This is the first real end-to-end worker command for approved tasks. It still is not autonomous merging. It gets approved work to a review PR and leaves evidence. That is the correct boundary for a human-control tower.
