# Demo Data Seeding and Reset

Aixion Control Tower now has deterministic backend demo data tooling.

This exists because a serious demo should not depend on manually creating projects, work orders, approvals, test runs, notifications, and audit events every time.

## Commands

Run from the backend directory.

Seed demo data:

```bash
python scripts/demo_data.py seed
```

Force reset then seed:

```bash
AIXION_PROFILE=demo python scripts/demo_data.py seed --force
```

Reset demo/test data:

```bash
AIXION_PROFILE=demo python scripts/demo_data.py reset
```

## Safety rule

Reset is allowed only for:

```text
demo
test
```

Reset is refused for:

```text
production
local
```

This is deliberate. A reset command that can wipe production is garbage engineering.

## Seeded entities

The seed command creates deterministic IDs for:

```text
project
idea
work order
approval request
test run
notification
audit event
```

Main seeded IDs:

```text
project_demo_aixion_control
idea_demo_mobile_approval
work_demo_mobile_approval
approval_demo_runtime_guard
test_demo_runtime_guard
notification_demo_runtime_guard
audit_demo_seeded
```

## Demo story

The seeded approval represents this flow:

```text
A demo builder agent proposes runtime guard visibility work.
The operator reviews the approval from mobile.
The approval includes file change summary, tests, rollback plan, and risk context.
Audit/recovery/readiness tooling can be shown around the same data set.
```

## Idempotency

Running seed repeatedly updates the same deterministic demo entities instead of creating endless duplicate demo data.

Use `seed --force` in demo/test profiles when you want a clean demo state.

## What this does not do

This tooling does not:

```text
seed production data
create real GitHub branches
send real FCM notifications
perform real restore drills
replace backend smoke validation
replace Android validation
```

## Recommended demo setup

```bash
git checkout main
git pull
cd backend
AIXION_PROFILE=demo python scripts/demo_data.py seed --force
python -m pytest
python scripts/generate_release_validation_summary.py
```

Then start the backend and run smoke validation:

```bash
AIXION_PROFILE=demo uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
python scripts/run_demo_smoke_validation.py --base-url http://127.0.0.1:8000
```

Hard truth: seeded data makes the demo repeatable. It does not make the product production-ready by itself.
