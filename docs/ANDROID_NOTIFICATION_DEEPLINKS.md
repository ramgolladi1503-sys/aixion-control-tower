# Android Notification Deep Links

This document describes the release-one Android push behavior for Aixion Control Tower.

## Purpose

Release one delivers approval-request pushes to the Android app. The client accepts an approval request payload, displays a local notification, and routes a tap to that approval's detail screen. Other push entity types are ignored by the current client.

## Supported push route

```text
entity_type=approval_request with a valid entity_id -> displays a local notification; tap opens Approval Detail for that approval
any other entity_type, including agent_task -> ignored; no local notification is displayed
invalid or incomplete approval_request payload -> ignored safely
```

`NotificationDeepLink` contains typed AgentTask routing support for app navigation, but `ControlTowerMessagingService` deliberately filters push messages and only displays notifications for `approval_request`. AgentTask push delivery and notification routing are deferred and are not part of the release-one notification workflow.

## Android behavior

When Firebase delivers a message with `entity_type=approval_request` and a valid `entity_id`:

```text
1. ControlTowerMessagingService validates the route and ignores non-approval push types.
2. For approval_request only, it builds a local Android notification with generic preview copy.
3. Tapping the notification launches MainActivity with approval deep-link extras.
4. MainActivity extracts NotificationDeepLink from the intent.
5. ControlTowerApp opens the existing Approval Detail route for the matching approval.
```

The notification preview does not expose project or task text from the untrusted push payload. The detail screen loads approval information from the backend after the user opens the app.

## Required FCM data payload

```json
{
  "notification_id": "notification_xxx",
  "entity_type": "approval_request",
  "entity_id": "approval_request_xxx"
}
```

An `agent_task` payload is not supported by the release-one push client and will be ignored, even though AgentTask links can be represented by the app's general deep-link model.

## Manual validation checklist

Use a real Android device or emulator with Google Play services and FCM configured.

```text
Android app installed with Firebase configuration
notification permission granted on Android 13+
FCM token registered with backend
backend creates/sends an approval_request notification
phone receives the approval notification
tapping it opens Approval Detail for the matching approval
non-approval push types (including agent_task) do not display a notification
```

For full real-device proof, use:

```text
docs/FCM_REAL_DEVICE_VALIDATION.md
docs/templates/FCM_REAL_DEVICE_EVIDENCE_TEMPLATE.md
```

## Current limits

```text
AgentTask push notifications and AgentTask notification deep links are deferred
No notification inbox deep-link fallback yet
No notification preference model yet
Eligible transient backend push-send failures use a durable, bounded retry queue
Permanent send failures and exhausted attempts remain terminal; no operator redrive path is documented
No instrumentation test for real FCM delivery yet
```

Hard truth: unit and routing tests do not prove end-to-end FCM delivery on a physical phone. That must be validated separately with real-device evidence.
