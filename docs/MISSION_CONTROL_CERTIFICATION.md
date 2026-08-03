# Mission Control V1 — Certification Boundary

## Verdict labels

```text
IMPLEMENTATION: COMPLETE ACROSS EIGHT DEFINED PHASES
DETERMINISTIC REPOSITORY CERTIFICATION: REQUIRED GREEN BEFORE MERGE
REAL DEPLOYED GITHUB EXECUTION: NOT YET CERTIFIED BY THIS PR
PUBLIC EXTERNAL-AGENT CONNECTIVITY: NOT CERTIFIED BY THIS PR
HIGH-ASSURANCE UNTRUSTED-CODE SANDBOX: NOT CLAIMED
AUTO-MERGE: NOT SUPPORTED
```

This document separates implemented product capability from evidence that still requires a deployed runtime and real operator exercise.

## Certified architecture boundary

```text
approved AgentTask + approved ApprovalRequest
-> exactly one active durable AgentRun
-> ordered AgentRunStep state machine
-> bounded lease and retry policy
-> policy-aware result verification
-> mobile operator visibility and step-boundary controls
-> GitHub feature branch / approved file / validation / PR adapters
-> immutable run events and evidence hashes
-> Prometheus and Grafana operational truth
-> controlled reliability fault injection
```

The approval decision is the scheduling hinge. Repeating the same approved decision does not create a second active run.

## Eight-phase implementation matrix

| Phase | Implemented evidence | Fail-closed property |
|---|---|---|
| 1. Contracts | Explicit run, step, event, decision, fault and status models | Illegal terminal restart is rejected |
| 2. Persistence | SQLite-backed durable runs, steps, events, faults and audit records | Restart does not erase run truth |
| 3. Recovery | Lease token, heartbeat endpoint, bounded retry, stale watchdog, idempotency key | Expired worker lease never becomes success |
| 4. Verification | PASS, transient retry, revision, human, policy block and permanent failure | Unknown or unsafe output does not default to pass |
| 5. Worker integration | Existing feature-branch, file application, isolated workspace, container validation and PR adapters | PR path is not reached after validation failure |
| 6. Mobile Mission Control | Run queue, summary, timeline, evidence, blockers and operator controls | Mobile displays backend truth and never creates fake local runs |
| 7. Observability | Prometheus endpoint, provisioned Grafana dashboard, run/step/retry/lease metrics | Operational availability is not mixed with model quality |
| 8. Fault and quality evidence | Scoped fault injection plus precision, recall, F1, Brier, ECE and entropy calculations | Fault injection is explicit, authenticated, counted and consumable |

## Concurrency model

The API process is the single owner of durable store mutation.

The scheduler process is stateless and interacts only through authenticated Mission Control APIs. It does not mount or modify the SQLite database. API persistence is serialized with a re-entrant process lock and SQLite busy timeout.

This avoids the unsafe pattern where two processes independently rewrite the complete key-value snapshot.

## Execution and lease model

The V1 execution API runs one governed step per request.

The effective lease budget is expanded to cover the maximum bounded validation-command budget plus overhead. Requests whose timeout cannot fit the maximum lease are rejected.

The heartbeat endpoint remains available for future external executors. The current stateless scheduler does not claim continuous asynchronous heartbeat during an in-process execution request.

## Operator-control model

Pause and cancel are step-boundary operations in V1.

When a step has a live execution lease or `RUNNING` state:

```text
pause -> HTTP 409
cancel -> HTTP 409
mobile action buttons -> hidden until durable step boundary
```

An expired lease must first be recovered by the watchdog. This prevents a late executor result from overwriting a concurrent pause or cancellation decision.

A terminal `FAILED`, `BLOCKED`, `SUCCEEDED` or `CANCELLED` run cannot be retried. Revised work requires a revised approval and a new run.

## Evidence-sealing model

The final `SEAL_EVIDENCE` step requires every prior step to be `SUCCEEDED`.

The evidence manifest includes:

```text
run id
AgentTask id
correlation id
ordered step types and states
verification decisions
attempt counts
per-step evidence hashes
output references
event count
```

A canonical SHA-256 digest becomes the run's durable `final_evidence_hash`. A deterministic test proves the default evidence-sealing executor persists this hash before the run is marked successful.

## Container boundary

`docker-compose.mission-control.yml` certifies the containerized API, stateless scheduler, Prometheus and Grafana control-plane path.

Real repository validation still depends on a runtime where the Aixion backend can access:

```text
Git credentials permitted for the approved repository
an isolated workspace
an available Docker runtime for fail-closed container validation
```

The Compose stack does not mount the host Docker socket and therefore does not claim high-assurance or unrestricted code execution. If the container runtime is unavailable, validation fails closed and no pull request is created.

For a real local execution demonstration, run the backend on a controlled host with Docker available, and run the stateless scheduler against that backend API.

## Required deterministic gates

Before this PR may be considered repository-certified:

```text
Backend lint passes
Full backend test suite passes
Android JVM tests pass
Android debug APK builds
Android release variant builds
Mission Control Compose configuration validates
API and stateless scheduler containers remain healthy
/health passes
/agent/runs/summary passes
/agent/runs/metrics passes
PR remains draft and unmerged
```

## Required real-runtime proof after merge decision

Repository certification does not replace a real operator demonstration. The next evidence campaign must prove:

1. Codex or another configured connector creates Agent Work.
2. A mobile operator approves exact scope, branch, files, tests and rollback.
3. Exactly one AgentRun is scheduled.
4. A transient failure enters `RETRY_WAIT` and recovers without duplicate mutation.
5. A policy violation enters permanent `BLOCKED` state.
6. A clean run creates exactly one feature-branch pull request after validation.
7. The Android timeline, audit trail, Prometheus metrics and final evidence hash agree.
8. Main remains untouched and auto-merge remains disabled.

## Honest final claim

After deterministic gates are green, the safe claim is:

> Aixion Control Tower contains a repository-certified resilient agent-run supervision layer with durable state, bounded recovery, policy-aware verification, mobile step-boundary control, fault injection, observability and evidence sealing.

The unsafe claim is:

> Aixion can already execute arbitrary untrusted agent work autonomously and safely in production.
