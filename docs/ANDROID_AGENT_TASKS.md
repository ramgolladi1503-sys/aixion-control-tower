# Android Agent Tasks Screen

This document describes the first Android surface for connected-agent work.

## Purpose

PR #78 added backend agent task ingestion. PR #79 linked agent tasks to approvals. PR #80 added the GPT Actions/OpenAPI contract. This screen makes that work visible from the mobile operator app.

The goal is simple:

```text
External agent creates task -> Android shows task -> operator sees status/timeline -> linked approval can be opened
```

## Android route

```text
Route.AgentTasks
Bottom navigation label: Agents
```

## Backend endpoints used

```text
GET /agent/tasks
GET /agent/tasks/{task_id}/events
```

The screen does not create tasks, approve tasks, or execute work. It is a read-first operator view.

## Screen shows

```text
total task count
active task count
waiting-for-approval count
provider
task status
title and goal
requested action
repository
branch preference
risk hint
linked approval action
timeline events
latest event actor/time/message
```

## Navigation behavior

If an agent task has a linked approval, Android can open the existing approval detail screen by approval ID.

## Scope

```text
Android only
No backend changes
No GPT Actions changes
No worker execution
No push notification lifecycle
No task creation form
No task cancellation
```

## Hard truth

Without this screen, connected-agent work exists only in backend APIs and docs. That is not a mobile control tower. This PR makes the connected-agent loop visible to the operator, but it still does not execute approved work. The next hard gap after this is worker contract and execution reporting.
