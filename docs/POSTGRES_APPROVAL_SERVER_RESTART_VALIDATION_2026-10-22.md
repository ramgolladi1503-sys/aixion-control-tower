# PostgreSQL Approval HTTP Server Restart Validation

**Date:** 2026-10-22  
**Scope:** Pre-billing connector recovery evidence for Slices 2 and 6  
**Status:** Focused local regression and exact-head GitHub backend validation passed; full slice gates remain open

## Scenario exercised

A test starts a real Uvicorn HTTP server process against a unique schema in the isolated local PostgreSQL test database. It then:

1. Creates an Antigravity approval over HTTP and simulates loss of the successful create response after the PostgreSQL commit.
2. Gracefully stops that server process.
3. Starts a new server process against the same schema and approves the persisted request over HTTP.
4. Stops that process and starts a third server process.
5. Retries the same Antigravity invocation and verifies that the hook returns `allow`.
6. Verifies one durable approval, one creation audit, one decision audit, and one delivery claim for that invocation.

The test owns its Uvicorn subprocesses and ephemeral loopback ports. Its PostgreSQL fixture creates and drops a uniquely named schema. It does not restart the shared PostgreSQL service.

## Validation

The focused route-flow suite was rerun from `backend/` against the isolated local PostgreSQL service at source revision `0c3447065b0acbfe442cb4b4fca4626de0ac1f89`:

```bash
AIXION_TEST_POSTGRES_DSN='host=/tmp/aixion-ct-pg.O8jwnb/socket dbname=aixion_test user=madhuram port=55432' \
  .venv/bin/python -m pytest -q tests/test_postgres_approval_route_flow.py
```

Result: **7 passed**, one existing Starlette/httpx deprecation warning. The suite includes HTTP server-restart recovery, recovery through a fresh API process, lost consume-response handling, workspace isolation, absence of SQLite projection, and an independent-process decision race.

GitHub CI run `39699619985` passed at the exact same source revision. Its backend validation, Android validation, container validation, and expected-blocked production startup guard all passed. Android debug APK build run `39699620016` also passed at that revision.

## Limits

This is local HTTP, PostgreSQL, and connector-hook test evidence. The reviewer decision is submitted by the test; no Android phone tap or native Antigravity session is involved. It does not prove full application PostgreSQL selection, the PostgreSQL server’s own restart behavior, hosted reachability, or production readiness. Slices 1, 2, and 6 remain open.

No application runtime code, new configuration key, database migration, cloud resource, billing state, or deployment changed.
