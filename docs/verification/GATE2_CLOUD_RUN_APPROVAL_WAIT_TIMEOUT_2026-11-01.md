# Gate 2 Cloud Run Approval-Wait Timeout Check — 2026-11-01

## Finding

The staging Cloud Run draft used a 60-second request timeout, while the application allows a pending approval to wait for up to 900 seconds on the synchronous MCP tool-call route. That configuration could terminate a valid approval wait before the app-level expiry.

Google Cloud documents a maximum Cloud Run request timeout of 3,600 seconds. Its serverless NEG backend timeout is fixed at 60 minutes and the `timeout_sec` field is unsupported for that backend type.

## Change

- Set the staging Cloud Run service timeout to 960 seconds: the 900-second approval TTL plus a 60-second response/cleanup margin.
- Make the local manifest validator reject non-integer timeouts, timeouts below 960 seconds, and values above the Cloud Run limit.
- Remove the unsupported `timeout_sec` field from the staging and production serverless-NEG backend-service drafts.
- Add regression coverage for short and over-limit service timeouts and for the unsupported Terraform field.

## Verification

- `python3 -m pytest backend/tests/test_cloud_run_manifest_validation.py -q`: 13 passed.
- `git diff --check`: passed.
- `terraform fmt -check ...`: not run successfully; Terraform CLI is not installed in this environment.

## Limits

This is a draft configuration correction only. It does not deploy Cloud Run, configure DNS or certificates, enable billing, establish PostgreSQL production authority, or prove push delivery or phone behavior. The production startup guard remains required until the production PostgreSQL gates pass.

## Run

From the repository root:

```sh
python3 -m pytest backend/tests/test_cloud_run_manifest_validation.py -q
terraform fmt -check deploy/gcp/cloud-run-load-balancer-staging.tf deploy/gcp/production/main.tf
```

No new configuration keys or database migration are introduced.
