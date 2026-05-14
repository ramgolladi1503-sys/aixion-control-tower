# Agent Worker File Patch Plan Dry Run

This document describes the first file-patch planning layer for approved AgentTasks.

## Purpose

Before a worker writes files into a repository, Aixion should prove it can validate the intended file changes and record evidence.

This dry-run does not write files. It validates the linked approval request's file changes and records the plan in the AgentTask timeline.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
loads the linked ApprovalRequest
validates file count
validates file paths
validates change types
validates new_content requirements
rejects secrets and dangerous paths
writes task timeline evidence
writes audit evidence
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
no command execution
no approval decision changes
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_file_plan.py --task-id agent_task_xxx
```

Run against first approved task:

```bash
python scripts/run_agent_worker_file_plan.py --first-approved
```

Print JSON:

```bash
python scripts/run_agent_worker_file_plan.py --task-id agent_task_xxx --json
```

## Allowed change types

```text
create
update
delete
```

Rules:

```text
create requires new_content
update requires new_content
delete must not include new_content
```

## Path safety rules

Rejected examples:

```text
absolute paths
../ traversal
empty paths
paths ending with /
.git
.github/workflows
node_modules
venv
.venv
__pycache__
.env
.env.local
.env.production
credentials.py
secrets.json
```

## Limits

```text
max file changes: 25
max new_content bytes per file: 250000
```

## Timeline event metadata

The dry-run event records:

```text
dry_run=true
worker_id=<worker id>
lease_token=<lease token>
approval_request_id=<approval id>
file_count=<count>
files=[path/change_type/has_new_content/new_content_bytes]
repository_mutated=false
files_written=false
plan_type=file_patch_dry_run
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_file_plan.py
python -m pytest
```

## Hard truth

This is still not repository execution. It is the safety proof before letting a worker write files. If this layer is skipped, the first real file-writing worker will mix path security, approval integrity, and GitHub mutation in one risky jump.
