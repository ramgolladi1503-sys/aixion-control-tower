# Real-Device FCM Validation

This checklist proves whether Aixion notifications actually reach a real Android device and open the correct screen.

## Why this exists

Backend notification records and Android deep-link code are not enough.

```text
Notification record created != push delivered
FCM accepted != user saw notification
App opened != routed to correct task/approval
```

This checklist captures real evidence for the full chain.

## Required setup

```text
Android device or emulator with Google Play services
Aixion Android app installed
notification permission granted on Android 13+
backend reachable by the device
AIXION_PROFILE configured intentionally
AIXION_AUTH_ENABLED configured intentionally
FCM_SERVER_KEY configured for push dispatch
Android device registered with backend through FCM token registration
```

## Network setup

Use one of these:

```text
same Wi-Fi backend: http://<laptop-lan-ip>:8000
public demo tunnel: https://<tunnel-host>
production backend: https://<production-domain>
```

Do not use `localhost` for real-device validation.

## Validation chain

### 1. Confirm Android app can reach backend

Evidence to capture:

```text
backend URL used by app
login/account screen success
GET /agent/tasks succeeds visually
screenshot of Agent Tasks screen loading
```

### 2. Confirm FCM token registration

Evidence to capture:

```text
Android log showing token refresh or registration
backend device registration response
registered device id/token prefix, never full token in public evidence
user id associated with device
```

### 3. Trigger AgentTask approval-needed notification

Create or use an AgentTask, then create linked approval.

Expected backend state:

```text
AgentTask status: WAITING_FOR_APPROVAL
AgentTask event: APPROVAL_CREATED
Notification entity_type: agent_task
Notification entity_id: <agent_task_id>
Notification push_status: SENT, SKIPPED, NO_DEVICE, or FAILED with reason
```

Pass criteria:

```text
phone receives notification
notification title/body are understandable
tapping notification opens Agents tab
matching AgentTask timeline is selected
```

### 4. Trigger approval decision notification

Approve or deny the linked approval from Android or backend.

Expected backend state:

```text
AgentTask event: APPROVED or DENIED
Notification entity_type: agent_task
Notification entity_id: <agent_task_id>
notification_id appears in AgentTask event metadata
```

Pass criteria:

```text
phone receives notification
tapping notification opens Agents tab
matching task timeline shows approval decision event
```

### 5. Trigger worker result notification

Run dry-run worker or append a worker status event such as DONE or FAILED.

Expected backend state:

```text
AgentTask event: RESULT_READY or FAILED
AgentTask status: DONE or FAILED
Notification entity_type: agent_task
Notification entity_id: <agent_task_id>
```

Pass criteria:

```text
phone receives notification
tapping notification opens Agents tab
matching task timeline shows result/failure event
```

### 6. Trigger approval_request deep link

Use or create a notification payload with:

```json
{
  "entity_type": "approval_request",
  "entity_id": "approval_request_xxx"
}
```

Pass criteria:

```text
phone receives notification
tapping notification opens Approval Detail
approval details are loaded or backend error is clear
```

## Push status interpretation

```text
PENDING   notification record exists before dispatch outcome
NO_DEVICE no registered device matched notification user
SKIPPED   FCM_SERVER_KEY missing or dispatch intentionally skipped
FAILED    backend attempted dispatch and failed
SENT      FCM accepted send request
```

Hard truth:

```text
SENT does not prove the user saw the notification.
Only the phone screenshot/logcat evidence proves user-visible delivery and routing.
```

## Evidence requirements

For each validation run, capture:

```text
date/time
device model
Android version
app version/backend commit
backend URL mode: LAN, tunnel, or production
AgentTask id
ApprovalRequest id
Notification id
push_status
Android logcat snippets
screenshots: notification shade, opened screen, selected timeline/detail
pass/fail result
known issues
```

Use:

```text
docs/templates/FCM_REAL_DEVICE_EVIDENCE_TEMPLATE.md
```

## Pass/fail rules

Pass only if:

```text
phone receives the notification
tap opens the app
tap routes to the expected screen
entity id matches the task/approval being tested
backend notification and task event metadata can be traced
```

Fail if:

```text
notification only appears in backend records
FCM push_status is SENT but phone shows nothing
notification opens app but wrong screen
notification opens correct screen but wrong task/approval
device token is missing or stale
backend is only reachable from emulator but not real device
```

## Known limits

```text
No automated FCM instrumentation test yet
No retry queue for FAILED pushes yet
No notification preference model yet
No full production observability dashboard yet
```

Hard truth: this checklist is the line between “we built notification code” and “we proved phone approval notifications work.” Do not claim the latter without evidence.
