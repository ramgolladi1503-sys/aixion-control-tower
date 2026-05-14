# Backend Demo Smoke Validation

Aixion Control Tower now has a backend demo smoke validation script.

This is a lightweight end-to-end API check for demo readiness. It is not a replacement for full CI, Android validation, security testing, deployment checks, backups, or monitoring.

## Goal

Before a demo, an operator should be able to prove that the core backend flow works against a running API:

```text
health
optional auth/bootstrap/login
runtime readiness
project creation
work order creation
approval creation
audit export
recovery snapshot export and validation
```

Without this, demo confidence is based on scattered manual calls and memory. That is weak engineering.

## Command

Run from the backend directory while the API server is running:

```bash
python scripts/run_demo_smoke_validation.py
```

By default it targets:

```text
http://127.0.0.1:8000
```

Custom base URL:

```bash
python scripts/run_demo_smoke_validation.py --base-url http://127.0.0.1:8000
```

JSON output:

```bash
python scripts/run_demo_smoke_validation.py --json
```

Custom output path:

```bash
python scripts/run_demo_smoke_validation.py \
  --output ../docs/release_reports/backend_demo_smoke_summary.md
```

## Auth behavior

The smoke validator first checks whether `/auth/me` works without a token. That supports demo/test profiles where auth is disabled.

If auth is enabled, it uses this order:

```text
1. use --token or AIXION_SMOKE_TOKEN if provided
2. attempt bootstrap registration with smoke owner credentials
3. attempt login with the same smoke owner credentials
```

Default smoke credentials:

```text
email: smoke-owner@example.com
password: SmokePassword123!
```

Override with:

```bash
python scripts/run_demo_smoke_validation.py \
  --email owner@example.com \
  --password 'your-password'
```

Or environment variables:

```text
AIXION_SMOKE_BASE_URL
AIXION_SMOKE_EMAIL
AIXION_SMOKE_PASSWORD
AIXION_SMOKE_TOKEN
AIXION_SMOKE_OUTPUT
```

## Output

Default report path:

```text
docs/release_reports/backend_demo_smoke_summary.md
```

The report includes:

```text
base URL
generated timestamp
PASS/FAIL decision
per-check result table
operator note
```

## Checks

The smoke validator runs these checks:

```text
health
runtime_readiness
core_demo_flow
audit_export
recovery_snapshot
```

The core demo flow creates:

```text
project
work order
approval request
```

## Exit code

```text
0 = all smoke checks passed
1 = one or more smoke checks failed
```

## What this does not prove

This script does not prove:

```text
Android UI works
GitHub execution worker works end-to-end
FCM push delivery works
production environment is safe
recovery restore works
monitoring or alerting works
load/performance readiness
```

## Hard truth

This script is intentionally boring. That is the point. A demo should not depend on clicking around and hoping the backend is alive. Run the smoke script, read the summary, then demo.
