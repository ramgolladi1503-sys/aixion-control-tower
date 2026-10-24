# PostgreSQL Verification Email One-Shot Worker

**Status:** Local worker implementation; production execution remains blocked by the Slice 1 PostgreSQL application-runtime gate.
**Scope:** A scheduled invocation claims and attempts at most one verification email. It does not create a Cloud Run Job, Cloud Scheduler job, secret, or other cloud resource.

## Why this worker is separate

Request-scaled services that can scale to zero are not a reliable owner for background polling. `AIXION_OUTBOX_DISPATCH_MODE=scheduled` disables both in-process push and email polling loops. The one-shot commands refuse to run in `inline` mode so an API loop and a scheduled worker cannot compete. This command is intended for a separately scheduled job after account registration, verification, and the email outbox all use the same PostgreSQL runtime. Do not use it to claim that the current application is production-ready.

## Local invocation

Run from `backend/` with the validated PostgreSQL environment and required secrets loaded through a protected local mechanism:

```sh
python scripts/run_postgres_email_dispatch_once.py
```

The command validates the startup environment before connecting. It returns `{"status":"idle"}` when dispatch is disabled or no due email exists, and `{"status":"processed"}` after it attempts one due email. A failure returns a non-zero exit status and a generic message. It never prints the recipient, verification code, SMTP response, secret, or database connection string.

## Required configuration

The command uses these settings:

- `AIXION_PROFILE=production` and the complete production environment required by `validate_startup_environment()`.
- `AIXION_OUTBOX_DISPATCH_MODE=scheduled`; production API instances must use this mode, while local profiles default to `inline` for compatibility.
- `AIXION_EMAIL_DISPATCH_ENABLED=true`.
- `AIXION_POSTGRES_DSN` for the migrated PostgreSQL database.
- `AIXION_EMAIL_OUTBOX_KEY`, `AIXION_EMAIL_SMTP_HOST`, `AIXION_EMAIL_FROM`, `AIXION_EMAIL_SMTP_USERNAME`, and `AIXION_EMAIL_SMTP_PASSWORD`.
- Existing email retry and lease settings, where non-default values are required.

The production startup guard currently rejects this checkout because the routed application is not fully PostgreSQL-backed. The script must fail at that guard before opening a database connection. Do not weaken the guard to run this worker.

## Delivery and scheduling semantics

- One invocation claims at most one row through `PostgresEmailOutboxRepository.claim_one_due()` and closes the PostgreSQL runtime afterward.
- Run push and email one-shot scripts as separate scheduled invocations. Keep scheduler concurrency bounded; database row leases and `FOR UPDATE SKIP LOCKED` arbitrate claims.
- The repository lease and `FOR UPDATE SKIP LOCKED` allow concurrent job attempts to claim different rows; scheduling should still cap concurrency and use one task per invocation.
- SMTP delivery is not exactly-once. If the SMTP server accepts a message but the worker loses the response, a later retry can send a duplicate. Verification codes remain time-limited and the user can request a replacement; monitor retry and dead-letter outcomes before enabling real email.
- Schedule only after registration and outbox production are bound to this same PostgreSQL authority. The HTTP service must not also run an independent competing SQLite email loop.
- Preserve the same `AIXION_EMAIL_OUTBOX_KEY` through migration and worker rollout so active encrypted queue entries remain decryptable. Rotate only with an explicit re-encryption or queue-drain procedure.

## Validation

Focused unit coverage is in `tests/test_run_postgres_email_dispatch_once.py`; PostgreSQL claim, retry, and ciphertext-clearing coverage is in `tests/test_postgres_email_outbox.py`. These tests do not certify SMTP-provider delivery or authorize a cloud schedule.

No schema migration is required. The new `AIXION_OUTBOX_DISPATCH_MODE` key defaults to `inline` outside production; production validation requires `scheduled`. A deployable schedule, Cloud Run Job manifest, runtime identity, and production rollout remain gated on the complete PostgreSQL application cutover and separate cloud authorization.
