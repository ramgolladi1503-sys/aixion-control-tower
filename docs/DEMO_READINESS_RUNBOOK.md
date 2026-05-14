# Aixion Control Tower Demo Readiness Runbook

This runbook is the single source of truth for running a clean Aixion Control Tower demo.

It exists because the product now has enough backend, Android, audit, recovery, readiness, and smoke-validation features that a demo can become messy if the operator improvises.

Hard truth: a strong product can still look weak if the demo operator fumbles setup, cannot explain limitations, or cannot prove the system is healthy.

## Demo objective

The demo should prove this product promise:

```text
A mobile operator can review, approve, diagnose, and validate AI/agent work before execution.
```

The demo must show:

```text
backend health/readiness
Android operator visibility
approval lifecycle
MCP pending/request visibility where available
audit trail/recovery readiness
demo smoke validation evidence
known limitations honestly
```

## Required pre-demo checks

Run these before presenting.

### 1. Pull latest main

```bash
git checkout main
git pull
```

### 2. Backend tests

```bash
cd backend
python -m pytest
```

Expected result:

```text
all backend tests pass
```

### 3. Android validation

```bash
cd ../mobile/android
./gradlew test assembleDebug
```

Expected result:

```text
BUILD SUCCESSFUL
```

### 4. Backend release validation summary

```bash
cd ../../backend
python scripts/generate_release_validation_summary.py
```

Expected output:

```text
docs/release_reports/backend_release_validation_summary.md
```

The summary should show:

```text
runtime readiness
migration status
recovery snapshot validation
audit retention/export policy
warnings/errors
```

### 5. Start backend API

Use the local/demo profile unless explicitly validating production behavior.

Example:

```bash
cd backend
AIXION_PROFILE=demo uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 6. Run demo smoke validation

In another terminal:

```bash
cd backend
python scripts/run_demo_smoke_validation.py --base-url http://127.0.0.1:8000
```

Expected result:

```text
Smoke decision: PASS
```

Default output:

```text
docs/release_reports/backend_demo_smoke_summary.md
```

### 7. Launch Android app

From Android Studio or CLI:

```bash
cd mobile/android
./gradlew assembleDebug
```

Then install/run the debug APK through your normal Android workflow.

## Demo sequence

Use this order. Do not jump around.

### Step 1 — Explain the product in one sentence

```text
Aixion Control Tower is a mobile approval console for supervising AI/agent execution before risky work is allowed to proceed.
```

### Step 2 — Show backend readiness evidence

Show either:

```text
GET /ops/readiness
```

or the Android Ops screen.

Point out:

```text
profile
auth_enabled
DB reachable
migrations applied
recovery snapshot available
GitHub configured boolean
FCM configured boolean
warnings/errors
```

Do not expose secrets. Only boolean configuration state is acceptable.

### Step 3 — Show Android Ops screen

Open:

```text
Ops
```

Explain:

```text
This is not just a mobile UI. It proves the backend can report runtime readiness to the operator surface.
```

### Step 4 — Show approval workflow

Open the review/approval flow and demonstrate:

```text
request details
risk summary
file/change summary
test plan
rollback plan
approve/deny decision path
```

If MCP pending data is available, show how pending tool calls remain visible before approval.

### Step 5 — Show audit trail

Open audit view or API export:

```text
GET /audit/export
```

Explain:

```text
audit export is bounded
retention policy is visible
sensitive detail keys are redacted
```

### Step 6 — Show recovery readiness

Use the release validation summary or recovery endpoints:

```text
GET /ops/recovery/export
POST /ops/recovery/validate
```

Explain:

```text
snapshots deliberately exclude sensitive credential/session collections
this is recovery state validation, not full credential restore
```

### Step 7 — Show smoke validation proof

Show:

```text
docs/release_reports/backend_demo_smoke_summary.md
```

Explain the smoke path:

```text
health
auth context
runtime readiness
project creation
work order creation
approval creation
audit export
recovery validation
```

## Demo success criteria

A demo is successful only if all of these are true:

```text
backend tests pass
Android test/build passes
runtime readiness is visible
smoke validation passes
approval lifecycle can be explained clearly
audit/recovery controls can be shown
known limitations are stated honestly
no secrets are displayed
```

If any of these fail, do not pretend the demo is clean. Call it a partial demo.

## Known limitations for demo

These must be stated if asked.

```text
This is not yet a fully deployed production SaaS.
Monitoring/alerting is not yet production-grade.
GitHub execution requires configured credentials and environment discipline.
FCM delivery requires real Firebase setup.
Recovery snapshot validation is not the same as full restore automation.
Audit retention policy exists, but automatic archival/deletion is not enabled yet.
Android shows runtime readiness, but full incident workflows are not implemented yet.
The smoke script proves core API flow, not every production edge case.
```

## Do not claim

Do not claim:

```text
production-ready SaaS
full compliance coverage
automated disaster recovery
complete observability
enterprise-grade monitoring
full security certification
zero-risk agent execution
```

Those claims are not earned yet.

## Safe claims

You can honestly claim:

```text
serious demo-grade mobile approval console
operator-facing runtime readiness
backend migrations foundation
safe recovery snapshot export and validation foundation
audit export and retention-policy foundation
release validation summary
demo smoke validation script
Android readiness visibility
approval lifecycle with auditability
MCP wait-mode approval foundation
```

## Demo recovery if something fails

If backend fails:

```bash
cd backend
python -m pytest
AIXION_PROFILE=demo uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
python scripts/run_demo_smoke_validation.py --base-url http://127.0.0.1:8000
```

If Android fails:

```bash
cd mobile/android
./gradlew test assembleDebug
```

If smoke validation fails:

```text
read the failed check
fix only that layer
rerun smoke validation
do not continue demo as if it passed
```

## Recommended demo script

Use this talk track:

```text
The problem is not that AI agents cannot do work. The problem is that risky work needs human approval, auditability, and recovery visibility before execution.

Aixion Control Tower gives the operator a mobile-first control surface. The backend tracks approval lifecycle, readiness, migrations, recovery snapshots, audit exports, and smoke validation. The Android app exposes the key operator views so the user is not tied to a laptop.

This demo shows the system health, approval control flow, audit evidence, and recovery readiness. It is not being presented as production SaaS yet; it is an elite MVP moving toward production-grade operation.
```

## Final pre-demo checklist

```text
[ ] main branch pulled
[ ] backend tests passed
[ ] Android tests/build passed
[ ] backend API running
[ ] release validation summary generated
[ ] smoke validation PASS
[ ] Android app installed/running
[ ] Ops screen loads
[ ] approval flow visible
[ ] audit/recovery explanation ready
[ ] known limitations ready
```
