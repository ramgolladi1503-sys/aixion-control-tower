# Aixion Mission Control — Resilient Agent Run Supervisor

## Product objective

Mission Control turns the existing approval-and-worker path into a durable execution supervision layer:

```text
AgentTask
-> linked mobile approval
-> AgentRun
-> ordered governed steps
-> bounded recovery or human intervention
-> verified pull request
-> sealed evidence chain
```

It does not permit direct edits to `main`, auto-merge, silent mutation, execution without approval, or pull-request creation after failed validation.

## V1 execution contract

A run is created only when both conditions are true:

1. the `AgentTask` status is `APPROVED`;
2. its linked `ApprovalRequest` status is `APPROVED`.

The durable step order is:

```text
VALIDATE_SCOPE
CREATE_BRANCH
APPLY_CHANGES
RUN_VALIDATION
CREATE_PULL_REQUEST
VERIFY_RESULT
SEAL_EVIDENCE
```

Every step records:

```text
run id
step id
correlation id
state transition
worker identity
attempt number
idempotency key
reason
input/output evidence hash
output reference
start/completion timestamps
```

## The eight implemented phases

### Phase 1 — Architecture and contracts

Implemented:

- explicit `AgentRun`, `AgentRunStep`, `AgentRunEvent`, and fault contracts;
- run and step state enums;
- deterministic transition tables;
- stable correlation and idempotency identifiers;
- documented safe and unsafe product claims.

Acceptance:

- illegal terminal restarts raise an error;
- run truth is not inferred from independent booleans;
- a run cannot exist without an approved task and approval.

### Phase 2 — Durable run, step, and event persistence

Implemented:

- SQLite-backed persistence using the existing KV boundary;
- durable maps for runs, steps, events, and fault configurations;
- detail/list/summary APIs;
- restart-safe evidence, leases, attempts, and states.

Acceptance:

- `store.persist()` and `store.load()` preserve all Mission Control entities;
- `store.reset()` clears them during isolated tests;
- creating the same active task run is idempotent.

### Phase 3 — State machine, leases, heartbeats, and stale recovery

Implemented:

- explicit run and step transition tables;
- bounded worker leases and lease tokens;
- heartbeat renewal;
- retry backoff;
- stale-lease watchdog recovery;
- duplicate-delivery event type;
- pause, resume, cancel, and bounded retry actions.

Acceptance:

- another worker cannot take an unexpired lease;
- an expired lease never becomes false success;
- retries stop at the configured attempt budget;
- terminal runs cannot restart.

### Phase 4 — Execution verifier and failure classification

Implemented deterministic outcomes:

```text
PASS
RETRY_TRANSIENT
NEEDS_REVISION
NEEDS_HUMAN
BLOCK_POLICY
FAIL_PERMANENT
```

The verifier separates:

- temporary infrastructure errors;
- validation or code regressions;
- ambiguous or conflicting side-effect state;
- policy violations;
- permanent failures.

The scope verifier checks approval state, repository format, protected branches, file plan, and validation plan.

Acceptance:

- policy blocks are not retried;
- failed tests do not become infrastructure retries;
- unknown failures do not default to success;
- evidence hashes are deterministic.

### Phase 5 — Recovery policy and worker integration

Implemented adapters to the existing governed worker path:

- safe GitHub feature-branch creation;
- approved file application;
- isolated workspace preparation;
- containerized validation;
- pull-request creation;
- result verification;
- evidence sealing.

`backend/scripts/run_mission_control_worker.py` polls eligible runs, recovers stale leases, and executes only the next governed step.

Recovery policy:

| Failure family | Outcome |
|---|---|
| timeout, rate limit, temporary 5xx, lost worker, container startup | bounded retry |
| test regression, incomplete objective, scope revision | human/revision required |
| protected branch, secret access, unapproved mutation | permanent policy block |
| unknown permanent error | fail closed |

Acceptance:

- a later step is never made ready before the previous step passes;
- PR creation occurs only after validation success;
- side-effect ambiguity requires reconciliation rather than blind retry;
- worker exceptions become governed outcomes, not process-wide false completion.

### Phase 6 — Android Mission Control Runs screen

Implemented:

- `Runs` navigation destination;
- run queue and operational summary;
- run detail with ordered step timeline;
- attempts, decisions, evidence hash, correlation id, blocker, branch/PR output;
- latest flight-recorder events;
- pause, resume, cancel, retry, and execute-next controls;
- authenticated Retrofit client and repository.

Acceptance:

- the phone shows backend truth only;
- backend errors do not create fake local runs;
- operator actions are recorded by backend APIs;
- terminal and blocked states do not show unsafe execution actions.

### Phase 7 — Observability

Implemented:

- Prometheus-compatible endpoint: `GET /agent/runs/metrics`;
- run counts by state;
- step counts by type and state;
- queue depth;
- retry attempts;
- duplicate deliveries;
- stale-lease recoveries;
- step durations;
- expired lease gauge;
- provisioned Grafana dashboard.

The Docker stack exposes:

```text
Aixion API: http://localhost:8000
Prometheus: http://localhost:9090
Grafana: http://localhost:3000
```

Operational metrics and AI/output quality are intentionally not combined into one score.

### Phase 8 — Fault injection, quality metrics, Docker, and certification

Implemented controlled fault types:

```text
WORKER_CRASH
GITHUB_TIMEOUT
DUPLICATE_CALLBACK
LOST_ACKNOWLEDGEMENT
VALIDATION_TIMEOUT
INVALID_AGENT_OUTPUT
STALE_LEASE
DATABASE_DISCONNECT
CONTAINER_FAILURE
FORBIDDEN_FILE_MUTATION
```

Faults are explicit, authenticated, scoped, counted, consumable, and disabled after their usage budget.

The evaluation library implements:

- confusion-matrix counts;
- precision;
- recall;
- F1;
- Brier score;
- expected calibration error;
- binary entropy.

The Docker stack contains:

- API;
- durable worker;
- shared SQLite volume;
- Prometheus;
- Grafana.

Certification requires backend tests, Android JVM tests, Android debug/release builds, Docker image build, and deterministic fault-recovery tests.

## API surface

```text
POST   /agent/runs
GET    /agent/runs
GET    /agent/runs/summary
GET    /agent/runs/metrics
GET    /agent/runs/{run_id}
GET    /agent/runs/{run_id}/steps
GET    /agent/runs/{run_id}/events
POST   /agent/runs/{run_id}/execute-next
POST   /agent/runs/{run_id}/heartbeat
POST   /agent/runs/{run_id}/pause
POST   /agent/runs/{run_id}/resume
POST   /agent/runs/{run_id}/cancel
POST   /agent/runs/{run_id}/retry
POST   /agent/runs/watchdog/recover-stale
GET    /agent/runs/faults
POST   /agent/runs/faults
DELETE /agent/runs/faults/{fault_id}
```

## Local Docker run

```bash
export GITHUB_TOKEN="..."  # required only for real GitHub mutations
docker compose -f docker-compose.mission-control.yml up --build
```

The demo profile disables auth to allow Prometheus scraping. Do not use that profile as production configuration.

## Manual flagship validation

1. Create or receive an `AgentTask` from Codex.
2. Link a mobile approval with a safe feature branch, exact file plan, tests, and rollback plan.
3. Approve it from Android.
4. Create its `AgentRun`.
5. Configure a one-use `WORKER_CRASH` or `GITHUB_TIMEOUT` fault.
6. Execute the affected step.
7. Confirm the run enters `RETRY_WAIT`, not success.
8. Run stale recovery or allow retry backoff to expire.
9. Execute the retry.
10. Inject `FORBIDDEN_FILE_MUTATION` on another test run and confirm permanent `BLOCKED` state.
11. Execute a clean run through validation and PR creation.
12. Confirm exactly one pull request, a sealed evidence hash, mobile timeline, Prometheus metrics, Grafana panels, and audit events.

## Safe claims

After deterministic tests and builds pass:

```text
Aixion has a durable, human-controlled agent-run supervision layer with bounded recovery,
step-level evidence, policy-aware failure classification, mobile operations, fault injection,
and operational observability.
```

After a real GitHub run also passes:

```text
Aixion can supervise an approved code-change run from feature-branch creation through
containerized validation and pull-request evidence while preserving operator control.
```

## Unsafe claims

Do not claim:

```text
arbitrary untrusted code is fully sandboxed;
auto-merge is supported;
production deployment is autonomous;
every external agent has been certified;
Docker socket access is safe by default;
a green deterministic suite proves public internet or Play Store readiness.
```
