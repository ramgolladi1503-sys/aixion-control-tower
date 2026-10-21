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
