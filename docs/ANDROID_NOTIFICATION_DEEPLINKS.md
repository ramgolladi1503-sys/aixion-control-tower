# Android Notification Deep Links

This document describes the first Android notification deep-link behavior for Aixion Control Tower.

## Purpose

Backend AgentTask notifications now include:

```text
notification_id
entity_type
entity_id
```

The Android app must use those fields when a push notification is tapped.

## Supported entity routes

```text
entity_type=agent_task        -> opens Agents screen and selects the matching task timeline
entity_type=approval_request  -> opens Approval Detail for the matching approval
unknown entity_type           -> opens app without special routing
```

## Android behavior

When Firebase receives a message with `entity_type` and `entity_id`:

```text
1. ControlTowerMessagingService builds a local Android notification.
2. Notification tap launches MainActivity with deep-link extras.
3. MainActivity extracts NotificationDeepLink from the intent.
4. ControlTowerApp routes based on entity type.
5. AgentTask links open the Agents tab and auto-select the matching timeline.
6. Approval links open the existing Approval Detail route.
```

## Required FCM data payload

```json
{
  "notification_id": "notification_xxx",
  "entity_type": "agent_task",
  "entity_id": "agent_task_xxx"
}
```

or:

```json
{
  "notification_id": "notification_xxx",
  "entity_type": "approval_request",
  "entity_id": "approval_request_xxx"
}
```

## Manual validation checklist

Use a real Android device or emulator with FCM configured.

```text
Android app installed
notification permission granted on Android 13+
FCM token registered with backend
backend creates AgentTask notification
phone receives push
tapping agent_task notification opens Agents tab
tapping agent_task notification selects matching task timeline
tapping approval_request notification opens Approval Detail
unknown entity_type does not crash the app
```

## Current limits

```text
No notification inbox deep-link fallback yet
No exact scroll-to-card behavior for selected AgentTask yet
No notification preference model yet
No retry queue for failed backend push sends yet
No instrumentation test for real FCM delivery yet
```

Hard truth: this PR makes notification taps useful. It still does not prove end-to-end FCM delivery on a physical phone. That must be validated separately with real device evidence.
