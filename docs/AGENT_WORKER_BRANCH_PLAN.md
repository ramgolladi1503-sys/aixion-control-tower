# Agent Worker Branch Plan Dry Run

This document describes the first branch-planning layer for approved AgentTasks.

## Purpose

Before a worker creates branches or mutates a repository, Aixion should prove it can safely plan the branch operation and record evidence.

This dry-run does not create a GitHub branch. It only validates and records the intended branch plan.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
validates repository format
validates branch name safety
rejects protected branches
generates a safe feature branch name when no branch preference exists
writes a task timeline NOTE with branch plan metadata
writes audit evidence
releases the worker lease
persists state
```

## What it does not do

```text
no GitHub API calls
no branch creation
no file patching
no commits
no pull request creation
no command execution
no approval decision changes
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_branch_plan.py --task-id agent_task_xxx
```

Run against first approved task:

```bash
python scripts/run_agent_worker_branch_plan.py --first-approved
```

Print JSON:

```bash
python scripts/run_agent_worker_branch_plan.py --task-id agent_task_xxx --json
```

## Safe branch rules

Allowed branch prefixes:

```text
feature/
fix/
chore/
docs/
test/
refactor/
```

Rejected examples:

```text
main
master
prod
production
release
hotfix/direct-prod
feature/../main
/feature/bad
feature//bad
```

## Repository rules

Repository must be in owner/repo format:

```text
ramgolladi1503-sys/aixion-control-tower
```

Invalid examples:

```text
repo-only
https://github.com/owner/repo
owner/repo/extra
```

## Generated branch format

If no `branch_preference` is provided, the branch planner generates:

```text
feature/agent-task-<short_task_id>-<title_slug>
```

Example:

```text
feature/agent-task-abcd1234-build-worker-branch-planner
```

## Timeline event metadata

The dry-run event records:

```text
dry_run=true
worker_id=<worker id>
lease_token=<lease token>
repository=<owner/repo>
planned_branch=<branch>
source_branch=main
branch_created=false
repository_mutated=false
plan_type=branch_creation_dry_run
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_branch_plan.py
python -m pytest
```

## Hard truth

This is still not repository execution. It is the last safety proof before adding a real GitHub branch creation step. That sequencing is intentional: prove branch names and audit evidence before letting a worker touch GitHub.
