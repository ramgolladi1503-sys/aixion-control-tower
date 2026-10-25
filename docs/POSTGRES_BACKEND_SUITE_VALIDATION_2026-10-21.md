# PostgreSQL Test-Database Suite Validation — 2026-10-21

**Scope:** Slice 1 validation evidence from [PRE_BILLING_WORK_LOCK.md](PRE_BILLING_WORK_LOCK.md).

## Run

The existing local PostgreSQL 17 test process was already live. It was not restarted. Tests used a disposable test database through the DSN below; no cloud service or paid resource was used.

```sh
AIXION_TEST_POSTGRES_DSN='postgresql://madhuram@localhost:55432/aixion_test?host=/tmp/aixion-ct-pg.O8jwnb/socket' \
  /tmp/aixion-ct-validation-20261021/bin/pytest backend/tests -q
```

Latest result after adding the cross-process regression test: **1000 passed, 0 skipped, 6 warnings in 27.80s**.

The focused auth repository, invite lookup, temporal session validation, PostgreSQL rate-limit route/repository, and email dispatch selection subset passed: **51 passed, 1 warning in 4.77s**.

The new `backend/tests/test_postgres_auth_cross_process.py` test creates a PostgreSQL auth session, commits owner-driven revocation in the test process, then starts a fresh spawned Python process and verifies the revoked session cannot authenticate. It passed independently (**1 passed in 0.74s**). Ruff and Python compilation passed for the new test.

## Device-session binding hardening — 2026-10-25

Migration `pg_0036_device_session_binding` binds each PostgreSQL push-device token to the active auth session that registered it. Dispatch targets require that exact session to remain unrevoked and unexpired. A device token re-registered by another active session is rebound and audited; logout makes the prior registration ineligible for future dispatch.

The migration intentionally preserves legacy registrations with `session_id=NULL`; it does not infer or backfill a session. PostgreSQL dispatch excludes those registrations until the authenticated app registers its current FCM token again. Roll out the migration before deploying the matching backend, then have signed-in clients re-register on their next app start. This can temporarily yield `NO_DEVICE` for accounts that have not re-registered; do not manually assign legacy tokens to sessions.

The migration runner executes DDL and ledger insertion in one PostgreSQL transaction under an advisory transaction lock. A failed statement rolls back the migration and ledger together. Repeated application verifies the same checksum and leaves `applied_at` unchanged; migration definitions must advance under a new ID rather than being edited after application.

## What this proves

The backend test suite in this checkout passes with PostgreSQL integration tests pointed at the available local PostgreSQL test database. The focused subset exercises PostgreSQL auth repository methods and the test route that selects PostgreSQL-backed auth rate-limit storage. The new process test proves that a committed PostgreSQL session revocation is visible to a fresh process using the auth repository.

## What this does not prove

The FastAPI application still selects SQLite-backed repositories and process-local compatibility state for production-authoritative domains. The PostgreSQL test DSN does not switch the complete routed application or all lifespan workers to PostgreSQL. The cross-process test exercises repositories directly, not two independent FastAPI app processes. These runs do not prove app-level PostgreSQL authority, full database rollback/restart recovery, production readiness, or the Slice 1 exit gate. Keep the production startup guard enabled.

## Warnings

The full run reported one Starlette/httpx `TestClient` deprecation warning and five uses of the deprecated `HTTP_422_UNPROCESSABLE_ENTITY` constant in registration tests. These warnings did not fail the suite and remain cleanup items; they do not change the persistence gate status.

## Configuration, migration, and rollout

No application configuration or migration changed for this validation. Keep cloud billing, cloud resources, deployment, and production database activation disabled. The next Slice 1 work remains binding application routes and lifespan workers to one runtime-owned PostgreSQL repository path, then proving route-level and cross-process behavior.
