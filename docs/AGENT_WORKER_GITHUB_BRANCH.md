# Agent Worker GitHub Branch Creation

This document describes the first real GitHub mutation step in the AgentTask worker path.

## Purpose

After branch planning, the worker can create a safe feature branch for an approved AgentTask.

This step creates only the branch. It does not write files, create commits, or open pull requests.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
validates repository and branch through the branch planner rules
checks target branch does not already exist
looks up source branch SHA
creates refs/heads/<safe-branch>
writes AgentTask timeline evidence
writes audit evidence
releases the worker lease
persists state
```

## What it does not do

```text
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
python scripts/run_agent_worker_github_branch.py --task-id agent_task_xxx
```

Run against first approved task:

```bash
python scripts/run_agent_worker_github_branch.py --first-approved
```

Use a non-main source branch:

```bash
python scripts/run_agent_worker_github_branch.py --task-id agent_task_xxx --source-branch develop
```

Print JSON:

```bash
python scripts/run_agent_worker_github_branch.py --task-id agent_task_xxx --json
```

## Required environment

```text
GITHUB_TOKEN
```

The token must have repository contents/ref permissions needed to read refs and create refs.

## Safety rules

The worker reuses the branch planner rules:

```text
repository must be owner/repo
branch must use safe prefixes such as feature/, fix/, chore/, docs/, test/, refactor/
main/master/prod/production/release are blocked
path traversal and malformed branch names are blocked
existing target branch is refused
```

## Timeline event metadata

The branch creation event records:

```text
worker_id=<worker id>
lease_token=<lease token>
repository=<owner/repo>
branch=<safe branch>
source_branch=<source branch>
source_sha=<sha>
branch_created=true
branch_url=<github branch url>
repository_mutated=true
files_written=false
commits_created=false
pull_request_opened=false
operation_type=github_branch_creation
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_github_branch.py
python -m pytest
```

Tests use a fake GitHub client and do not call GitHub.

## Hard truth

This is the first real GitHub mutation. It still does not deliver a code change. It only creates the safe branch that later file-writing and commit steps can use.
