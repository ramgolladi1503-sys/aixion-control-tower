# FCM Real-Device Evidence Template

Use this template for every real-device notification validation run.

## Run metadata

```text
Date/time:
Tester:
Backend commit:
Android commit:
App version:
Backend URL mode: LAN / tunnel / production
Backend URL:
AIXION_PROFILE:
AIXION_AUTH_ENABLED:
FCM_SERVER_KEY configured: yes/no
```

## Device metadata

```text
Device model:
Android version:
Google Play services available: yes/no
Notification permission granted: yes/no
Battery/background restrictions disabled: yes/no
```

## Device registration evidence

```text
FCM token prefix only:
Backend registered device id:
Associated user id/email:
Registration timestamp:
Relevant logcat lines:
```

## AgentTask approval-needed test

```text
AgentTask id:
ApprovalRequest id:
Notification id:
Task event id:
Expected entity_type: agent_task
Expected entity_id:
Backend push_status:
Phone notification visible: yes/no
Tap opened Agents tab: yes/no
Matching task timeline selected: yes/no
Screenshots captured: yes/no
Result: PASS/FAIL
Notes:
```

## Approval decision test

```text
Decision: APPROVED / DENIED / REVISION_REQUESTED
AgentTask id:
ApprovalRequest id:
Notification id:
Task event id:
Backend push_status:
Phone notification visible: yes/no
Tap opened Agents tab: yes/no
Matching task timeline shows decision: yes/no
Screenshots captured: yes/no
Result: PASS/FAIL
Notes:
```

## Worker result test

```text
Worker event: EXECUTING / READY_FOR_PR / FAILED / DONE
AgentTask id:
Notification id:
Task event id:
Backend push_status:
Phone notification visible: yes/no
Tap opened Agents tab: yes/no
Matching task timeline shows worker event: yes/no
Screenshots captured: yes/no
Result: PASS/FAIL
Notes:
```

## ApprovalRequest deep-link test

```text
ApprovalRequest id:
Notification id:
Expected entity_type: approval_request
Expected entity_id:
Backend push_status:
Phone notification visible: yes/no
Tap opened Approval Detail: yes/no
Correct approval displayed: yes/no
Screenshots captured: yes/no
Result: PASS/FAIL
Notes:
```

## Final result

```text
Overall result: PASS/FAIL
Blocking issues:
Non-blocking issues:
Follow-up PRs needed:
Evidence location:
```

## Hard truth

Do not mark this run as PASS unless the notification is visible on the phone and the tap routes to the correct screen for the matching entity id.
