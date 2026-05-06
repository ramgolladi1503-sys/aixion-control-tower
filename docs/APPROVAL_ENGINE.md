# Approval Engine

## Purpose

The approval engine controls whether AI-agent proposed work can move forward.

It must never behave like a dumb `yes/no` button. It must consider risk, evidence, tests, branch safety, rollback, and audit requirements.

## Approval States

```text
PENDING_REVIEW
APPROVED
REJECTED
REVISION_REQUESTED
BLOCKED
APPLIED
TESTS_RUNNING
TESTS_PASSED
TESTS_FAILED
READY_FOR_PR
```

## Allowed User Decisions

- approve
- reject
- request revision
- approve with tests
- approve into branch
- block permanently

## Required Approval Payload

Every approval request must contain:

- project id
- work order id
- title
- summary
- action type
- affected files
- diff or patch summary
- risk level
- risk explanation
- test plan
- rollback plan
- agent name

## Blocking Rules

Approval must be blocked when:

- target branch is `main` or `master`
- file path appears to contain secrets or credentials
- diff is missing for a file change
- critical-risk change lacks a test plan
- critical-risk change lacks rollback instructions
- agent gives no reason for the change

## Decision Audit

Every decision must write an audit event with:

- actor
- decision
- timestamp
- request id
- previous status
- new status
- reason
- risk snapshot
