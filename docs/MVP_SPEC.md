# MVP Specification

## MVP Mission

Build the smallest serious version of Aixion Control Tower that proves controlled AI-assisted execution from idea to approved branch-ready change.

## MVP Workflow

1. User captures an idea.
2. System creates or updates a project.
3. System generates a structured project spec.
4. System creates an agent work order.
5. Agent proposes one or more file changes.
6. Risk engine scores the request.
7. User reviews summary, diff, risk, tests, and rollback plan.
8. User approves, rejects, or requests revision.
9. Approved patch is marked ready for branch application.
10. Test run is recorded.
11. Audit log stores the full decision trail.

## MVP Screens

- Home Dashboard
- Idea Command Chat
- Projects
- Agent Work Orders
- Approval Inbox
- Approval Detail
- Diff Viewer
- Risk Review
- Test Results
- Audit Log

## MVP Backend Modules

- projects
- ideas
- work_orders
- approvals
- risk
- test_runs
- audit
- github placeholder

## MVP API Capabilities

- create project
- list projects
- submit idea
- generate work order from idea
- create approval request
- list approval requests
- approve request
- reject request
- request revision
- calculate risk score
- record test run
- list audit events

## MVP Acceptance Criteria

The MVP is acceptable only when these flows work:

### Flow 1: Idea to Work Order

A user submits an idea and the system generates a work order with goal, context, risk, tasks, tests, and rollback plan.

### Flow 2: Work Order to Approval

A work order can produce an approval request containing affected files, diff, risk, tests, and actions.

### Flow 3: Approval Decision

The user can approve, reject, or request revision, and every decision creates an audit event.

### Flow 4: Risk Policy Enforcement

Blocked files such as `.env`, credentials, secrets, and direct `main` branch edits cannot be approved.

## Out of Scope for MVP

- auto-merge
- production deployment
- direct main branch edits
- full autonomous agent execution
- credential management
- billing
- multi-user enterprise permissions

## MVP Default Safety Mode

Strict Mode:

- all code changes require manual approval
- high-risk changes require tests and rollback plan
- blocked files cannot be approved
- every action must be audited
