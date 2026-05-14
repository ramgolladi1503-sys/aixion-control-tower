# Backend Release Validation Summary

Aixion Control Tower now has a backend release validation summary generator.

This is not deployment. It is a repeatable operator-facing artifact that summarizes whether the backend looks safe enough for demo or release review based on the operational foundations already in the product.

## Goal

Before a backend build is treated as demo-ready or release-candidate-ready, operators should have one markdown artifact that summarizes:

```text
runtime readiness
database migration status
recovery snapshot validation
audit retention/export policy
secret configuration booleans
warnings and errors
```

Without this, release confidence depends on scattered logs, memory, and vibes. That is not good enough.

## Generator

Run from the backend directory:

```bash
python scripts/generate_release_validation_summary.py
```

Default output:

```text
docs/release_reports/backend_release_validation_summary.md
```

Custom output:

```bash
python scripts/generate_release_validation_summary.py \
  --output ../docs/release_reports/backend_release_validation_summary.md
```

## Summary decision

The generated summary returns one of:

```text
PASS
REVIEW_REQUIRED
```

`PASS` means the internal readiness and recovery checks passed and no hard errors were detected.

`REVIEW_REQUIRED` means an operator must inspect the generated warnings/errors before treating the build as releasable.

## Included checks

The summary includes runtime readiness data:

```text
profile
auth_enabled
readiness_status
db_reachable
github_token_configured
fcm_server_key_configured
```

It includes migration data:

```text
migrations_applied
expected_migration_ids
applied_migration_ids
```

It includes recovery data:

```text
recovery_snapshot_available
recovery_format_version
recovery_snapshot_valid
recovery_warnings
recovery_errors
```

It includes audit policy data:

```text
audit_retention_policy_version
audit_online_retention_days
audit_archive_retention_days
audit_deletion_job_enabled
audit_export_max_limit
```

## What this does not do

This foundation deliberately does not add:

```text
deployment
binary packaging
Docker image publishing
cloud release upload
signed artifacts
production monitoring
release approval workflow
Android changes
```

## Operator workflow

Recommended backend release validation flow:

```bash
cd backend
python -m pytest
python scripts/generate_release_validation_summary.py
```

Then review:

```text
docs/release_reports/backend_release_validation_summary.md
```

## Hard truth

This report does not make a bad build good. It only prevents sloppy release thinking by forcing readiness, migrations, recovery, and audit controls into one repeatable review artifact.
