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
from the project-scoped mapping, mutable image tags, missing resource limits, and an
invalid startup probe.

## Validation

The preflight tests passed **9/9**. The full backend suite ran with the isolated
PostgreSQL test database and passed **1,351 tests, 0 skipped, 6 existing warnings**.
Ruff, Python compilation, and `git diff --check` passed for the preflight files.
Running the default command on the checked-in template returns the expected
`NOT APPLY-READY` result because the deployment values remain unresolved.

## Limits and rollout

A passing result means only that the local manifest shape and listed invariants
passed. It does not verify GCP project ownership, IAM/API enablement, Secret Manager
contents or permissions, Cloud SQL connectivity, application readiness, push/email
delivery, budget controls, or billing approval. It does not apply or deploy resources.

The checked-in manifest remains deliberately blocked until the full application
PostgreSQL runtime and the staging account/project, image, runtime identity, secrets,
and cost review are ready. No cloud credentials are needed to run this preflight.
`PyYAML` is a development-only dependency used by this offline validation tool. No
application runtime configuration key or database migration was added.
