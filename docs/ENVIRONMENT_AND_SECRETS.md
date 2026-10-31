# Environment and Secrets Policy

Aixion Control Tower now has explicit environment profiles and startup validation. The goal is simple: local and demo work should stay easy, but production must not boot with demo-grade assumptions.

## Profiles

Set `AIXION_PROFILE` to one of:

```text
local
demo
test
production
```

If `AIXION_PROFILE` is omitted, the backend uses `local`.

## Local

Expected use: normal developer machine work.

Defaults:

```text
AIXION_AUTH_ENABLED=true
AIXION_DB_PATH=runtime/aixion_control_tower.sqlite3
```

Local may run without GitHub execution or push notification secrets. Those features will fail or skip when called without their specific secrets.

## Demo

Expected use: safe non-production demo data and mobile UI walkthroughs.

Defaults:

```text
AIXION_AUTH_ENABLED=false
AIXION_DB_PATH=runtime/aixion_control_tower_demo.sqlite3
```

Demo is intentionally less strict. Do not point production traffic at the demo profile.

## Test

Expected use: automated backend tests.

Defaults:

```text
AIXION_AUTH_ENABLED=false
AIXION_DB_PATH=runtime/aixion_control_tower_test.sqlite3
```

Test exists for repeatable validation, not real user data.

## Production

Production must fail fast when required configuration is missing or unsafe.

Required:

```text
AIXION_PROFILE=production
AIXION_POSTGRES_DSN=<secret-managed PostgreSQL DSN with sslmode=verify-full>
GITHUB_TOKEN=<configured in secret manager or deployment environment>
AIXION_FCM_PROJECT_ID=<Firebase project ID>
GOOGLE_APPLICATION_CREDENTIALS=<protected service-account JSON path, or use workload identity>
AIXION_EMAIL_DISPATCH_ENABLED=true
AIXION_EMAIL_SMTP_HOST=<approved SMTP relay>
AIXION_EMAIL_SMTP_PORT=587
AIXION_EMAIL_SMTP_USERNAME=<secret-manager reference>
AIXION_EMAIL_SMTP_PASSWORD=<secret-manager reference>
AIXION_EMAIL_FROM=<verified sender address>
AIXION_EMAIL_OUTBOX_KEY=<64 hex characters from 32 random bytes>
AIXION_AUTH_RATE_LIMIT_HMAC_KEY=<64 hex characters from 32 random bytes>
```

Production rules:

```text
AIXION_AUTH_ENABLED must not be false
AIXION_POSTGRES_DSN must be configured and use full TLS certificate/hostname verification
AIXION_DB_PATH must not be configured; it is SQLite-only
GitHub execution secret must be present
FCM project ID must be present; runtime ADC credentials must be able to send FCM messages
Verification email dispatch must use SMTP STARTTLS, paired credentials, and a protected outbox encryption key
Authentication rate limits must use the separate HMAC key; raw IP addresses and email identifiers are not stored in rate-limit buckets
```

Production startup remains blocked: PostgreSQL repositories and a staging
probe exist, but HTTP routes and background workers still use the SQLite-backed
application store. A DSN does not enable PostgreSQL routing. Do not send hosted
traffic to this build or enable production email dispatch until the full
PostgreSQL runtime, transactional outboxes, tenant isolation, and recovery gates
are implemented and certified. `AIXION_DB_PATH` remains local/demo/test only.

## Secret handling rules

Do not commit secret values to the repository.

Do not place production secrets in docs, tests, fixtures, screenshots, or PR descriptions.

Use deployment environment variables or a secret manager.

Rotate secrets immediately if a token appears in logs, screenshots, issue comments, PR comments, or local shell history shared publicly.

## What this does not solve yet

This is environment and secret discipline only.

It does not add deployment.
It does not add monitoring.
It does not add backup or restore drills.
It does not replace SQLite with PostgreSQL.
It does not add runtime secret rotation.

FCM uses the HTTP v1 API with short-lived OAuth credentials from Google Application
Default Credentials. Prefer workload identity in hosted environments. For local or
on-prem deployments, point `GOOGLE_APPLICATION_CREDENTIALS` at a protected service
account file and grant only the Firebase Cloud Messaging send permission required by
the deployment. Do not put the service-account JSON in the repository.

Hard truth: this makes accidental unsafe production startup harder, not impossible. Real production readiness still needs deployment policy, managed database, backups, observability, and incident handling.
