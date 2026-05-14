# Agent Worker GitHub File Application

This document describes the worker step that applies approved file changes to an existing safe feature branch.

## Purpose

After a safe branch exists, the worker can apply approved file changes from the linked `ApprovalRequest.files` list.

This step writes files through GitHub's Contents API. GitHub creates commits for those file operations. This step still does not open a pull request or merge anything.

## What it does

```text
claims an approved AgentTask through the transactional worker claim helper
loads the linked ApprovalRequest
validates branch safety through branch planner rules
validates file changes through file planner rules
checks the target branch exists
creates new files when change_type=create
updates existing files when change_type=update
deletes existing files when change_type=delete
records commit sha per file operation
writes AgentTask timeline evidence
writes audit evidence
releases the worker lease
persists state
```

## What it does not do

```text
no branch creation
no pull request creation
no merge
no approval decision changes
no command execution
```

## Command

Run by task ID:

```bash
cd backend
python scripts/run_agent_worker_github_files.py --task-id agent_task_xxx
```

Run against first approved task:

```bash
python scripts/run_agent_worker_github_files.py --first-approved
```

Print JSON:

```bash
python scripts/run_agent_worker_github_files.py --task-id agent_task_xxx --json
```

## Required environment

```text
GITHUB_TOKEN
```

The token must be able to read and write repository contents on the target branch.

## Safety rules

This worker reuses:

```text
branch planner validation
file patch planner validation
transactional worker claim guard
```

Refusals include:

```text
unsafe branch
missing branch
unsafe file path
blocked secret-like filenames
create when file already exists
update when file does not exist
delete when file does not exist
duplicate file paths
missing new_content for create/update
delete with new_content
```

## Timeline event metadata

The file application event records:

```text
worker_id=<worker id>
lease_token=<lease token>
approval_request_id=<approval id>
repository=<owner/repo>
branch=<safe branch>
files_written=<count>
files_deleted=<count>
commits_created=<count>
items=[path/change_type/commit_sha]
repository_mutated=true
pull_request_opened=false
operation_type=github_file_application
```

## Validation

Run:

```bash
cd backend
python -m pytest tests/test_agent_worker_github_files.py
python -m pytest
```

Tests use a fake GitHub client and do not call GitHub.

## Hard truth

This step writes approved files and creates commits through GitHub's Contents API. It still does not open a pull request, run validation after writing, or merge anything. Those must remain separate explicit worker steps.
