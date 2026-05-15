# Containerized Validation Runner

This document defines PR 113: containerized validation for approved AgentTask worker execution.

## Purpose

Before this PR, the worker validation runner executed allowlisted validation commands through local subprocess execution in the configured working directory or prepared workspace. That was useful for MVP proof, but it was not production-safe isolation.

PR 113 adds a Docker-backed validation executor and wires the approved AgentTask orchestrator to use containerized validation by default when a test executor is not injected.

## Default behavior

The approved AgentTask worker flow now defaults to:

```text
branch creation
file application
isolated workspace preparation
containerized validation
PR creation only if validation passes
```

If the container runtime is unavailable, validation fails closed and the PR step is not reached.

## Container policy

Default policy:

```text
runtime: docker
image: python:3.12-slim
workdir: /workspace
workspace mount: read-only
network: disabled
root filesystem: read-only
tmpfs /tmp: enabled
fail closed on runtime unavailable: true
```

## CLI flags

Disable container validation explicitly:

```bash
python -m app.agent_worker_orchestrator --task-id <task_id> --no-container-validation
```

This should be used only for local debugging or test-only workflows.

## What gets recorded

The worker records:

```text
agent_worker.container_validation_configured audit event
container_validation_required metadata on orchestration events
validation command artifacts through the existing validation runner
fail-closed result when runtime is unavailable
```

## What this PR does not solve

```text
no custom image per repository yet
no dependency cache
no artifact upload/download storage
no Kubernetes/Firecracker isolation
no secrets injection into containers
no network allowlist
```

## Hard truth

This is a serious safety improvement, but it is still Docker-level isolation. It is enough for a strong MVP and demo-grade production story, not a high-assurance sandbox for untrusted arbitrary code.
