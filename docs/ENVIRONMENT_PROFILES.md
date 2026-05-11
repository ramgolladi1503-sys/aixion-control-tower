# Environment Profiles

This document defines the runtime profile model for Mobile Approval Console / Aixion Control Tower.

The goal is simple: local development, demo mode, tests, and production-like runs must not rely on scattered, half-remembered environment variables.

## Profile variable

Use:

```bash
AIXION_PROFILE=local|demo|test|production
```

If omitted, the backend defaults to:

```text
local
```

## Supported profiles

| Profile | Auth default | DB default | Purpose |
| --- | --- | --- | --- |
| `local` | enabled | `runtime/aixion_control_tower.sqlite3` | Normal local development with auth on by default. |
| `demo` | disabled | `runtime/aixion_control_tower_demo.sqlite3` | Fast LAN/demo mode where approval proof is more important than login. |
| `test` | disabled | `runtime/aixion_control_tower_test.sqlite3` | Script/test validation with isolated data. |
| `production` | enabled | `runtime/aixion_control_tower.sqlite3` | Production-like behavior. Still not a full deployment story. |

## Override rules

Explicit environment variables always override profile defaults.

Supported overrides:

```bash
AIXION_AUTH_ENABLED=true|false
AIXION_DB_PATH=runtime/custom.sqlite3
```

Example:

```bash
AIXION_PROFILE=demo AIXION_AUTH_ENABLED=true uvicorn app.main:app --reload
```

This uses demo profile defaults but turns auth back on.

## Local development

Default:

```bash
cd backend
uvicorn app.main:app --reload
```

Equivalent explicit version:

```bash
cd backend
AIXION_PROFILE=local uvicorn app.main:app --reload
```

Expected behavior:

```text
auth enabled
DB path runtime/aixion_control_tower.sqlite3
```

## Fast demo mode

Use:

```bash
cd backend
bash scripts/run_demo_server.sh
```

The script defaults to:

```text
AIXION_PROFILE=demo
AIXION_AUTH_ENABLED=false
AIXION_DB_PATH=runtime/aixion_control_tower_demo.sqlite3
```

This is the correct mode for fast MCP wait-mode proof where login/session behavior is not the focus.

## Auth-enabled demo mode

Use this when proving Android Account login/register/session behavior:

```bash
cd backend
AIXION_PROFILE=demo AIXION_AUTH_ENABLED=true uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Expected behavior:

```text
demo database path
bearer auth required
Android must login/register through Acct before protected calls work
```

## Test/script mode

Validation scripts should use:

```bash
AIXION_PROFILE=test
```

The MCP approval validation script sets this automatically unless explicitly overridden.

Expected behavior:

```text
auth disabled by default
isolated test database path unless AIXION_DB_PATH overrides it
```

## Production-like mode

Use:

```bash
cd backend
AIXION_PROFILE=production AIXION_DB_PATH=/safe/persistent/path/aixion.sqlite3 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Expected behavior:

```text
auth enabled
explicit persistent DB path strongly recommended
no reload flag
```

Hard truth: this is still not a complete production deployment. It does not solve secrets, TLS, database migrations, monitoring, role-based permissions, backups, or infra hardening.

## Android API URL profile

Android currently uses a Gradle property:

```bash
-PAIXION_API_BASE_URL=http://YOUR_HOST:8000/
```

Default emulator URL:

```text
http://10.0.2.2:8000/
```

Physical phone LAN URL example:

```bash
cd mobile/android
./gradlew assembleDebug -PAIXION_API_BASE_URL=http://192.168.1.42:8000/
```

Hard rule: the URL must end with `/` because Retrofit requires a trailing slash.

## Claim policy

Safe claim after this profile layer:

```text
Backend environment profiles are centralized and documented for local, demo, test, and production-like runs.
```

Unsafe claim:

```text
Production deployment is solved.
```

That is false until deployment, secrets, monitoring, TLS, backups, and role permissions are implemented.

## Failure interpretation

| Failure | Meaning | Action |
| --- | --- | --- |
| Unknown profile | `AIXION_PROFILE` typo or unsupported value | Use `local`, `demo`, `test`, or `production`. |
| Invalid auth value | `AIXION_AUTH_ENABLED` is not boolean-like | Use `true` or `false`. |
| Auth required during demo | Profile/override enabled auth | Login through Android `Acct` or set auth false for fast demo. |
| Data appears in wrong DB | Wrong profile or `AIXION_DB_PATH` override | Print env and verify DB path before demo. |
| Phone cannot reach backend | Android URL/network issue, not profile issue | Prove `/health` from phone browser first. |

## Brutal truth

Profiles do not make the product production-ready. They only remove configuration confusion. That is necessary, but not sufficient.
