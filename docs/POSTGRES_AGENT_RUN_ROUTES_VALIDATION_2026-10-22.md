# PostgreSQL AgentRun HTTP route validation

**Date:** 2026-10-22  
**Scope:** Slice 1 partial route evidence  
**Status:** Focused route suite and full PostgreSQL-enabled backend suite passed; Slice 1 remains open

## Coverage

A dedicated HTTP route suite binds AgentRun, AgentTask, approval, and membership
repositories from one `ApplicationRepositories` bundle to the FastAPI app. It uses
a UUID-named PostgreSQL schema and drops that schema during fixture teardown.

The suite verifies:

- AgentRun creation and durable list, detail, steps, events, and summary reads.
- Cross-workspace list/detail isolation.
- Pause, resume, and cancel transitions with durable AgentRun steps, events, and audit records.
- No AgentRun, step, event, or audit projection into the SQLite process store.

It does not exercise execute-next, capability/trust transaction binding, fault
configuration, the default application lifespan's PostgreSQL selection, or a native
agent-to-phone approval round trip.

## Validation

Focused command, from `backend/`:

```bash
AIXION_TEST_POSTGRES_DSN='host=/tmp/aixion-ct-pg.O8jwnb/socket dbname=aixion_test user=madhuram port=55432' \
  .venv/bin/python -m pytest -q tests/test_postgres_agent_run_routes.py
```

Result: **2 passed**, with one existing Starlette/httpx deprecation warning. Ruff and
`git diff --check` passed for the new test file.

The full backend suite with PostgreSQL integration enabled passed **1,357 tests,
0 skipped, 6 existing warnings** in 70.62 seconds.

## Limits

The test injects repositories into the in-process FastAPI app; it does not prove
application-wide PostgreSQL runtime selection. SQLite remains the default local
store, production startup remains blocked, and Slice 1's route, worker, trust,
migration/cutover, and cross-process exit requirements remain open. No runtime
configuration key, database migration, cloud resource, billing state, or deployment
changed.
