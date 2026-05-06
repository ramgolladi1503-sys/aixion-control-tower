# Mobile App MVP

The mobile app is the command and approval surface for Aixion Control Tower.

## MVP Screens

1. Home Dashboard
2. Idea Command Chat
3. Projects
4. Agent Work Orders
5. Approval Inbox
6. Approval Detail
7. Diff Viewer
8. Risk Review
9. Test Results
10. Audit Log

## MVP Screen Responsibilities

### Home Dashboard

Shows active projects, pending approvals, failed tests, and recent audit events.

### Idea Command Chat

Captures raw ideas and sends them to the backend for project planning and work order creation.

### Approval Inbox

Shows pending, blocked, approved, rejected, and revision-requested approval requests.

### Approval Detail

Shows request summary, agent reason, risk assessment, affected files, tests, rollback plan, and decision buttons.

### Diff Viewer

Shows mobile-readable diffs with risky file badges.

### Risk Review

Shows risk level, reasons, required actions, and approval blockers.

## MVP Mobile Rule

The mobile app should never hide risk. High-risk and critical requests must force the user into detail review before approval.
