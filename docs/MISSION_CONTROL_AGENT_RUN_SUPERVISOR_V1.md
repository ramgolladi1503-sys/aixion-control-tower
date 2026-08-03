# Aixion Mission Control — Agent Run Supervisor V1

Status: implementation started

Tracking issue: #187

## Purpose

Aixion Mission Control turns approved Agent Work into a durable, recoverable, policy-governed execution run. It extends the existing approval, connector, GitHub worker, validation, and audit paths instead of introducing a parallel toy service.

## V1 flow

```text
AgentTask / Work Order
-> linked Approval
-> approved scope
-> AgentRun
-> ordered AgentRunSteps
-> lease-based worker execution
-> deterministic step verification
-> PASS / RETRY / HUMAN / BLOCK / FAIL
-> pull request or intervention
-> sealed evidence timeline
```

## V1 supported steps

```text
VALIDATE_APPROVAL_SCOPE
CREATE_BRANCH
APPLY_CHANGES
RUN_VALIDATION
EVALUATE_RESULT
CREATE_PULL_REQUEST
SEAL_EVIDENCE
```

## Non-negotiable boundaries

```text
No direct main edits.
No auto-merge.
No silent retries of unsafe mutations.
No PR creation when required validation fails or is skipped.
No fake offline AgentRun records.
No autonomous approval of revised scope.
No production-readiness claim without deployed and real-device evidence.
```

## Canonical run states

```text
CREATED
SCHEDULED
RUNNING
RETRY_WAIT
NEEDS_HUMAN
PAUSED
BLOCKED
SUCCEEDED
FAILED
CANCELED
```

## Canonical step states

```text
PENDING
READY
RUNNING
RETRY_WAIT
NEEDS_HUMAN
BLOCKED
SUCCEEDED
FAILED
CANCELED
```

## Terminal states

Run terminal states:

```text
BLOCKED
SUCCEEDED
FAILED
CANCELED
```

Step terminal states:

```text
BLOCKED
SUCCEEDED
FAILED
CANCELED
```

## Required durable entities

### AgentRun

Minimum fields:

```text
id
agent_task_id
work_order_id
approval_id
project_id
source_provider
objective
approved_scope_json
state
correlation_id
current_step_id
created_at
started_at
completed_at
paused_at
cancel_requested_at
failure_class
failure_reason
version
```

### AgentRunStep

Minimum fields:

```text
id
run_id
step_type
sequence
state
attempt_count
max_attempts
idempotency_key
lease_owner
lease_token_hash
lease_acquired_at
lease_expires_at
heartbeat_at
input_evidence_hash
output_evidence_hash
result_json
failure_class
failure_reason
started_at
completed_at
version
```

### AgentRunTransitionEvent

Minimum fields:

```text
id
run_id
step_id
correlation_id
actor_type
actor_id
previous_state
new_state
reason_code
reason
attempt_number
input_evidence_hash
output_evidence_hash
created_at
```

### AgentRunAttempt

Minimum fields:

```text
id
run_id
step_id
attempt_number
worker_id
started_at
heartbeat_at
completed_at
outcome
error_class
error_message
result_hash
```

## Transition requirements

- All transitions go through one transition service.
- Illegal transitions fail closed.
- Transition checks include expected version to prevent lost updates.
- Every accepted transition writes an immutable event.
- Run state is derived from step truth where possible.
- Retry count is bounded.
- Correlation ID remains stable from source AgentTask through PR creation.
- Human actions require role checks and current-state checks.

## Failure classes

```text
TRANSIENT_DEPENDENCY
RATE_LIMITED
WORKER_LOST
VALIDATION_REGRESSION
OBJECTIVE_INCOMPLETE
SCOPE_REVISION_REQUIRED
POLICY_VIOLATION
AUTHORIZATION_FAILURE
DUPLICATE_CONFLICT
PERMANENT_EXECUTION_FAILURE
```

## Recovery decisions

```text
TRANSIENT_DEPENDENCY -> RETRY_TRANSIENT
RATE_LIMITED -> RETRY_TRANSIENT
WORKER_LOST -> RETRY_TRANSIENT after lease expiry
VALIDATION_REGRESSION -> NEEDS_REVISION
OBJECTIVE_INCOMPLETE -> NEEDS_HUMAN
SCOPE_REVISION_REQUIRED -> NEEDS_HUMAN
POLICY_VIOLATION -> BLOCK_POLICY
AUTHORIZATION_FAILURE -> BLOCK_POLICY
DUPLICATE_CONFLICT -> BLOCK_POLICY
PERMANENT_EXECUTION_FAILURE -> FAIL_PERMANENT
```

## Verifier outcomes

```text
PASS
RETRY_TRANSIENT
NEEDS_REVISION
NEEDS_HUMAN
BLOCK_POLICY
FAIL_PERMANENT
```

Deterministic policy checks take precedence over any optional model-based judgment.

## Eight delivery phases

### Phase 1 — Durable run truth

- Models and persistence
- Transition service
- Read APIs
- Focused state-machine tests

### Phase 2 — Resilient worker

- Leases and heartbeats
- Stale lease recovery
- Bounded retry and backoff
- Idempotent mutation checkpoints

### Phase 3 — Execution verifier

- Per-step contracts
- Evidence manifests
- Deterministic outcome classification
- Optional human/LLM evaluation behind policy gates

### Phase 4 — Recovery policy and operator actions

- Pause, resume, cancel
- Retry step
- Approve or reject revision
- Audit propagation

### Phase 5 — Android Mission Control

- Runs overview
- Run detail and timeline
- Evidence and artifacts
- Controlled operator actions

### Phase 6 — Observability and flight recorder

- Structured events
- Metrics, traces, dashboards, alerts
- End-to-end correlation

### Phase 7 — Fault injection and certification

- Crash, timeout, duplicate callback, lost acknowledgement, stale lease, database failure, container failure, forbidden mutation
- Deterministic recovery tests

### Phase 8 — Evaluation metrics and portfolio proof

- Human-labelled verifier evidence
- Precision, recall, false approvals, false blocks
- Calibration and Brier score only where probabilistic outputs exist
- Dockerized local stack
- Flagship real-device demo

## First implementation PR scope

The first implementation PR must remain narrow:

```text
AgentRun state enum
AgentRunStep state enum
allowed transition tables
pure transition validator
run-state derivation
focused unit tests
no worker behavior change
no Android change
no database migration until existing persistence conventions are inspected
```

This sequencing prevents an unreviewable cross-stack change and establishes deterministic state truth before leases, retries, UI, and metrics are added.

## Completion standard

The program is complete only when:

- all eight phases are merged;
- focused and broad tests pass;
- real-device Android behavior is validated;
- crash recovery is demonstrated without duplicate branch or PR creation;
- required validation cannot be skipped;
- the evidence timeline is complete;
- safe and unsafe product claims are documented.
