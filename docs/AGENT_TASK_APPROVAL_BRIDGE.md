# Agent Task Approval Bridge

Agent tasks now have a first bridge into the existing approval engine.

## Endpoint

```text
POST /agent/tasks/{task_id}/approval
```

This creates an `ApprovalRequest` from an existing `AgentTask`, links the approval ID back to the task, and moves the task to:

```text
WAITING_FOR_APPROVAL
```

## Decision propagation

When a linked approval is decided:

```text
APPROVED approval -> AgentTask status APPROVED
DENIED approval   -> AgentTask status DENIED
revision request  -> AgentTask status PLANNING
```

The bridge also writes timeline events and audit events.

## What this enables

This is the first real connection between the connected-agent inbox and the mobile approval system.

Example flow:

```text
ChatGPT creates an AgentTask
Aixion creates a linked ApprovalRequest
Android operator approves or denies
AgentTask status updates from the approval decision
```

## What this does not do yet

```text
No Codex/GitHub worker execution
No GPT Actions/OpenAPI contract
No Android Agent Tasks screen
No agent-task push notification lifecycle
No automatic approval generation policy engine
```

Hard truth: PR #78 created the inbox. This bridge makes the inbox enter the approval system. It still does not execute work.
