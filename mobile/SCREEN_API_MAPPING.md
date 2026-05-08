# Screen to API Mapping

This document maps the Android MVP screens to the FastAPI backend endpoints.

## 1. Home Dashboard

### APIs

```text
GET /projects
GET /approvals
GET /test-runs
GET /audit
```

### Derived Metrics

- active projects = count(projects)
- pending approvals = approvals where status = PENDING_REVIEW
- blocked requests = approvals where status = BLOCKED
- failed tests = test_runs where status contains failed/fail
- recent activity = last audit events

## 2. Projects Screen

### APIs

```text
GET /projects
POST /projects
```

### Card Fields

- project name
- description
- mode
- pending approvals count
- blocked request count
- last audit activity

## 3. Command Chat Screen

### MVP APIs

```text
POST /ideas
POST /work-orders
```

### Flow

1. User selects project.
2. User writes idea.
3. App calls `POST /ideas`.
4. App creates structured work order through `POST /work-orders`.

### Future APIs

```text
POST /chat/messages
POST /project-generator/specs
POST /agent-work-orders/generate
```

## 4. Work Orders Screen

### APIs

```text
GET /work-orders
POST /work-orders
```

### Actions

- Review work order
- Create approval request
- Send to agent runner later

## 5. Approval Inbox

### APIs

```text
GET /approvals
```

### Tabs

- Pending: PENDING_REVIEW
- Blocked: BLOCKED
- Approved: APPROVED
- Rejected: REJECTED
- Revision: REVISION_REQUESTED

## 6. Approval Detail

### APIs

```text
GET /approvals/{approval_id}
POST /approvals/{approval_id}/decision
```

### Decision Payload

```json
{
  "decision": "approve",
  "reason": "Tests and rollback plan are present."
}
```

Supported decisions:

```text
approve
reject
revise
```

## 7. Diff Viewer

### Source

Diff is currently embedded in approval request file changes.

### Future API

```text
GET /approvals/{approval_id}/diff
```

## 8. Test Runs

### APIs

```text
GET /test-runs
POST /test-runs
```

## 9. Audit Trail

### APIs

```text
GET /audit
```

## MVP UI Blocking Rules

The mobile app must mirror backend safety:

- If approval status is BLOCKED, hide approve button.
- If `risk.required_actions` is non-empty, disable approve button.
- If risk level is HIGH or CRITICAL, require detail review before decision.
- If files have diffs, show Diff Viewer shortcut.
