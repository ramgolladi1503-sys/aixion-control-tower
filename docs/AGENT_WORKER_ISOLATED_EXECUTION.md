# Isolated AgentTask Execution Workspace

This document describes the PR 102 isolation hardening for approved AgentTask orchestration.

## Purpose

The previous worker orchestrator created a branch and applied files remotely, but validation still ran in the configured local working directory. That was unsafe because validation could pass against stale or unrelated local files.

PR 102 changes the default path so validation runs inside an isolated temporary checkout of the exact task branch.

## New behavior

The approved AgentTask worker flow is now:

1. create task branch
2. apply approved files to task branch
3. prepare isolated temporary workspace
4. checkout the task branch in that workspace
5. run validation commands inside that workspace
6. open PR only after validation passes
7. cleanup workspace after success or failure

## Workspace evidence

The worker records safe metadata only:

- workspace_success
- workspace_isolated
- workspace_cleaned
- workspace_path_redacted
- repository_path_redacted

Raw local paths are deliberately not stored in AgentTask event metadata or audit details.

## Failure behavior

If workspace preparation fails, validation does not run, the PR does not open, orchestration ends as FAILED, cleanup is attempted, and an audit event records the workspace preparation failure.

If validation fails, the PR does not open, orchestration ends as FAILED, and cleanup is attempted.

## Compatibility mode

The orchestrator keeps a legacy opt-out flag for local debugging only. Real worker execution should keep isolation enabled.

## Validation

Run the orchestrator tests and full backend test suite before merging.

## Hard truth

This closes the biggest correctness hole in the worker path: validation now targets the branch that will become the PR. It is still not a full production sandbox. The next level should add containerized execution, artifact capture, network policy, and resource limits.
