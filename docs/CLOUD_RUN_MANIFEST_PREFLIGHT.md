# Cloud Run staging manifest preflight

The staging manifest is a template and is not apply-ready. Before replacing any
values or running an apply command, complete the local preflight:

```bash
cd backend
python -m pip install -e '.[dev]'
python scripts/validate_cloud_run_manifest.py
```

The checked-in template is expected to fail while it contains unresolved
placeholders. To validate a completed candidate without modifying the template:

```bash
python scripts/validate_cloud_run_manifest.py /path/to/reviewed-cloud-run-staging.yaml
```

The validator fails closed for invalid YAML, unresolved placeholders, non-production
profile settings, missing PostgreSQL/outbox requirements, SQLite configuration,
plaintext secret values, duplicate environment names, Secret Manager aliases missing
from the project-scoped mapping, mutable image tags, image sources outside the
project's expected Artifact Registry repository, nonnumeric secret versions, missing
resource limits, an invalid startup probe, and a missing or increased Cloud Run
service `maxScale` above the reviewed value of `1`. This instance limit bounds the
service's configured scale; it does not cap Cloud SQL or project-wide costs.

## Validation

Run the focused tests after changing the validator or manifest:

```bash
cd backend
python -m pytest tests/test_cloud_run_manifest_validation.py -q
python -m ruff check scripts/validate_cloud_run_manifest.py tests/test_cloud_run_manifest_validation.py
```

The current focused suite passed **9/9**, including explicit missing-cap and
increased-cap rejection cases. The checked-in template returned the expected
`NOT APPLY-READY` result because deployment values remain unresolved. Record exact
test counts and the PostgreSQL test DSN status with each reviewed candidate; a local
shape check is not a substitute for the repository's full CI suite.

## Limits and rollout

A passing result means only that the local manifest shape and listed invariants
passed. It does not verify GCP project ownership, IAM/API enablement, Secret Manager
contents or permissions, Cloud SQL connectivity, application readiness, push/email
delivery, budget controls, or billing approval. It does not apply or deploy resources.

The checked-in manifest remains deliberately blocked until the full application
PostgreSQL runtime and the staging account/project, image, runtime identity, secrets,
and cost review are ready. The `maxScale: "1"` requirement is a temporary staging
exposure control, not a hard project spend limit. No cloud credentials are needed to
run this preflight. `PyYAML` is a development-only dependency used by this offline
validation tool. No application runtime configuration key or database migration was
added.

## Dispatcher Jobs and Cloud Scheduler preflight

The separate draft `deploy/gcp/cloud-run-jobs-staging.yaml` describes one push
dispatcher Job and one email dispatcher Job. Each runs the corresponding bounded
one-shot script with one task, parallelism one, zero Cloud Run task retries, a
300-second task timeout, resource limits, a job-specific runtime identity, and a
digest-pinned image placeholder. The push Job receives only its PostgreSQL
connection secret and uses the job identity's Application Default Credentials for
FCM. The email Job receives its PostgreSQL connection and SMTP/outbox secrets. Do
not copy service-account JSON keys into either Job.

`deploy/gcp/cloud-scheduler-staging.tf` defines separate schedules with unresolved
cron/timezone/project/region inputs. Each scheduler trigger uses OAuth to call the
Cloud Run Jobs v2 `:run` API endpoint. A dedicated Scheduler identity receives
`roles/run.invoker` only on the named push and email jobs through job-level IAM
members; no project-wide Invoker grant is intended. Configured Scheduler retries
are set to zero to avoid retry amplification and defer failed work to a later
owner-approved tick. Cloud Scheduler still delivers at least once and rare
duplicate requests can occur even with retries disabled. This does not establish
exactly-once external delivery; database leasing and idempotency remain required.

Run the offline preflight and focused tests from `backend/`:

```bash
python scripts/validate_cloud_run_jobs_manifest.py
python -m pytest tests/test_cloud_run_jobs_manifest_validation.py -q
python -m ruff check scripts/validate_cloud_run_jobs_manifest.py tests/test_cloud_run_jobs_manifest_validation.py
```

The checked-in job and Terraform templates must report **NOT APPLY-READY** while
placeholders remain. A clean local result for a reviewed candidate proves only the
static shape invariants: two separate jobs, matching Cloud Run project and region
defaults across the Terraform and Job YAML, same project/region/digest-pinned image,
validly shaped job-specific service-account IDs, secret boundaries, one task, zero
task retries, OAuth target, and job-level invoker grants. It does not validate
Terraform variable overrides, provider schema, effective IAM, service-agent
permissions, project/billing state, secret existence, worker startup, schedule
adequacy, cloud cost, or actual delivery. Local formatting and provider-schema checks
are allowed during pre-billing with the committed, read-only provider lockfile:

```bash
cd deploy/gcp
terraform fmt -check cloud-scheduler-staging.tf
terraform init -backend=false -input=false -lockfile=readonly
terraform validate -no-color
```

The backendless initialization downloads the locked provider but does not configure
remote state or require cloud credentials. It is the same no-cloud validation run by
repository CI. Do not run `terraform plan`, `terraform apply`, authenticate to a
cloud account, enable APIs, or perform paid resource operations during this
pre-billing phase. These local checks do not make the unresolved templates
apply-ready.

**Worker validation status (2026-10-22):** both dispatcher scripts now use a
strict worker-scoped production validator. It requires the production profile and
scheduled mode, rejects an explicit SQLite database path, and validates PostgreSQL
plus only the provider configuration for the selected job. The API-wide production
startup validator remains unchanged and continues to reject startup until the whole
application is PostgreSQL-authoritative. Local worker settings and dispatch tests
passed; this does not make the jobs deployable because outbox producers and the
complete application runtime remain on split SQLite/PostgreSQL authority. Keep the
templates blocked, do not use a local/demo profile, and do not distribute all API
secrets to both jobs.

Google references: [Cloud Run Jobs YAML](https://docs.cloud.google.com/run/docs/reference/yaml/v1#job),
[Cloud Run Job secrets](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets),
[create Cloud Run Jobs](https://docs.cloud.google.com/run/docs/create-jobs),
[schedule Cloud Run Jobs with Cloud Scheduler](https://docs.cloud.google.com/run/docs/execute/jobs-on-schedule),
[Cloud Scheduler delivery and retry semantics](https://docs.cloud.google.com/scheduler/docs/overview),
and [Cloud Run IAM permissions](https://docs.cloud.google.com/run/docs/reference/iam/permissions).
