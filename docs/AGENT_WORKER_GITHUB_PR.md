# Agent Worker GitHub Pull Request Opening

This document describes the worker step that opens a GitHub pull request for an approved AgentTask branch.

## Purpose

After a safe branch exists and approved file changes are applied, the worker can open a pull request for human review.

This step opens only the PR. It does not approve, merge, or deploy anything.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
validates repository and branch through the branch planner rules
checks the head branch exists
checks there is no existing open PR for head -> base
opens a GitHub pull request
moves AgentTask to READY_FOR_PR
writes AgentTask timeline evidence
writes audit evidence
releases the worker lease
persists state
```

## What it does not do

```text
no merge
no self-approval
no branch creation
no file writes
no command execution
no deployment
no approval decision changes
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_github_pr.py --task-id agent_task_xxx
```

Run against first approved task:

```bash
python scripts/run_agent_worker_github_pr.py --first-approved
```

Use a non-main base branch:

```bash
python scripts/run_agent_worker_github_pr.py --task-id agent_task_xxx --base-branch develop
```

Print JSON:

```bash
python scripts/run_agent_worker_github_pr.py --task-id agent_task_xxx --json
```

## Required environment

```text
GITHUB_TOKEN
```

The token must be able to read branches and open pull requests.

## Safety rules

The worker reuses branch planner validation:

```text
repository must be owner/repo
head branch must use safe prefixes such as feature/, fix/, chore/, docs/, test/, refactor/
main/master/prod/production/release are blocked as head branches
path traversal and malformed branch names are blocked
missing head branch is refused
existing open PR for head -> base is refused
```

## Timeline event metadata

The PR creation event records:

```text
worker_id=<worker id>
lease_token=<lease token>
approval_request_id=<approval id>
repository=<owner/repo>
head_branch=<safe branch>
base_branch=<base branch>
pull_request_number=<number>
pull_request_url=<url>
pull_request_opened=true
merged=false
operation_type=github_pull_request_creation
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_github_pr.py
python -m pytest
```

Tests use a fake GitHub client and do not call GitHub.

## Hard truth

This gets the worker output to human review. It still does not prove the PR is correct, passing CI, approved, or merged. Those remain separate checks and human/operator decisions.
