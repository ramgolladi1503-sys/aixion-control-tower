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
AIXION_DB_PATH=<explicit production database path>
GITHUB_TOKEN=<configured in secret manager or deployment environment>
FCM_SERVER_KEY=<configured in secret manager or deployment environment>
```

Production rules:

```text
AIXION_AUTH_ENABLED must not be false
AIXION_DB_PATH must be explicitly configured
AIXION_DB_PATH must not point at demo or test database filenames
GitHub execution secret must be present
FCM push secret must be present
```

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

Hard truth: this makes accidental unsafe production startup harder, not impossible. Real production readiness still needs deployment policy, managed database, backups, observability, and incident handling.
