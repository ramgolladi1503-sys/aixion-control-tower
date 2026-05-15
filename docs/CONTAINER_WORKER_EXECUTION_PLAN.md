# Container Worker Execution Plan

This document defines the PR 104 container execution planning contract.

## Purpose

Aixion worker validation is now branch-correct and evidence-rich, but it is not yet a hard runtime sandbox. PR 104 does not run containers. It defines the container execution policy that a future sandbox runner must obey.

## Scope

This PR adds dry-run planning only:

- container execution policy model
- safe default policy
- policy validation
- dry-run worker helper
- CLI wrapper
- AgentTask event evidence
- audit evidence
- tests for safe and unsafe policy settings

No container is started in this PR.

## Default policy

Policy version:

```text
aixion-container-execution-policy-v1
```

Default image:

```text
python:3.12-slim
```

Allowed images:

```text
python:3.12-slim
node:20-bookworm-slim
gradle:8.7-jdk17
```

Runtime limits:

```text
network_mode=none
memory_limit_mb=1024
cpu_limit=1.0
timeout_seconds=300
read_only_rootfs=true
drop_all_capabilities=true
no_new_privileges=true
workspace_mount_mode=rw
```

Explicitly blocked:

```text
host networking
privileged mode
Docker socket mounts
non-allowlisted images
workdir outside /workspace
memory/cpu/timeout outside policy bounds
```

## Dry-run command

```bash
cd backend
python scripts/run_agent_worker_container_plan.py --task-id agent_task_xxx --json
```

## Evidence recorded

The dry-run event records:

```text
dry_run=true
container_execution_planned=true
container_started=false
container_policy=<policy object>
plan_type=container_execution_policy_dry_run
```

The audit event records:

```text
agent_worker.container_execution_plan_completed
container_started=false
container_policy=<policy object>
```

## Why not run containers yet?

Actually running containers is a separate risk boundary. It needs careful handling for mounted workspace paths, output artifact collection, timeout enforcement, process cleanup, image availability, and host Docker permissions. Doing that in the same PR as the policy contract would be bad engineering.

## Next production step

The next PR can implement the actual sandbox runner against this contract. It should fail closed if the runtime environment cannot enforce the policy.
