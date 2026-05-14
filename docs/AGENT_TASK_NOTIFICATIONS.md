# Agent Task Notification Lifecycle

This document describes the first AgentTask-specific notification lifecycle.

## Purpose

Aixion already had generic notifications and FCM dispatch. This lifecycle connects AgentTask milestones to notifications so the Android operator can be alerted when connected-agent work needs attention.

## Notification triggers

AgentTask notifications are created for these events:

```text
approval created for agent task
linked approval approved
linked approval denied
linked approval revision requested
worker reports EXECUTING
worker reports READY_FOR_PR
worker reports FAILED
worker reports DONE
```

AgentTask notifications are not created for noisy intermediate states such as `TESTING`.

## Entity model

AgentTask lifecycle notifications use:

```text
entity_type=agent_task
entity_id=<agent_task_id>
user_id=<task.created_by_user_id>
```

This is deliberate. The generic approval notification can still point to `approval_request`, but the connected-agent lifecycle should point the operator back to the Agent Tasks screen and timeline.

## Timeline metadata

When a notification is created from an AgentTask event, the task event metadata includes:

```text
notification_id=<notification_id>
```

That gives the Android timeline, backend audit trail, and notification inbox a shared reference.

## Approval-created notification

When `POST /agent/tasks/{task_id}/approval` creates a linked approval:

```text
AgentTask status -> WAITING_FOR_APPROVAL
AgentTask event  -> APPROVAL_CREATED
Notification     -> Agent task approval needed
```

## Approval decision notifications

When the linked approval decision is propagated:

```text
APPROVED            -> Agent task approved
DENIED              -> Agent task denied
REVISION_REQUESTED  -> Agent task needs revision
```

## Worker status notifications

When a worker appends an event with a status that needs operator attention:

```text
EXECUTING    -> Agent task executing
READY_FOR_PR -> Agent task ready for PR review
FAILED       -> Agent task failed
DONE         -> Agent task done
```

## Current limits

```text
FCM transport still depends on FCM_SERVER_KEY
missing FCM_SERVER_KEY marks push as SKIPPED
no Android deep-link routing into a specific task yet
no notification preference model yet
no retry queue for failed push sends yet
```

Hard truth: this PR does not magically make phone push perfect. It connects the AgentTask lifecycle to the existing notification system and proves the backend emits the right notification records and metadata.
