# Backend Database Migrations

Aixion Control Tower has versioned migration runners for its local SQLite store and PostgreSQL schema. These runners do not mean the application has completed its PostgreSQL persistence cutover.

The production profile still fails closed while application routes and workers use SQLite-backed process state. Do not point production traffic at this build.

## Current storage model

The MVP still stores application state in SQLite through a compact key-value table:

```text
kv_store(entity_type, entity_id, payload)
```

This keeps the current API and tests stable while avoiding a risky mid-MVP rewrite into a normalized relational schema.

## Migration metadata

Migration state is tracked in:

```text
schema_migrations
```

Columns:

| Column | Purpose |
| --- | --- |
| `migration_id` | Stable migration identifier, for example `0001_baseline_kv_store`. |
| `name` | Human-readable migration name. |
| `applied_at` | UTC timestamp for when the migration was applied. |

## Current migration

```text
0001_baseline_kv_store
```

Purpose:

```text
create baseline kv_store table if missing
record baseline schema metadata
preserve existing kv_store rows
```

## Startup behavior

On backend store startup:

```text
open SQLite database
ensure schema_migrations exists
fail if database contains unknown newer migrations
apply pending known migrations in order
load state from kv_store
```

The runner is intentionally idempotent. Running it multiple times should not duplicate migration metadata or rewrite existing state.

## Future-version safety

If the database contains a migration id that this backend version does not know, startup fails instead of continuing.

That is deliberate.

Running old backend code against a newer database can silently corrupt state. Failing loudly is safer than pretending compatibility exists.

## Tests

Focused tests live in:

```text
backend/tests/test_database_migrations.py
```

Run:

```bash
cd backend
python -m pytest tests/test_database_migrations.py
```

The tests cover:

```text
fresh DB receives baseline migration
migration runner is idempotent
existing persisted state survives migration startup
unknown newer migration fails safely
migration metadata persists
```

## What the SQLite runner does not solve

The SQLite store and its migrations do not provide:

- PostgreSQL application authority or route/worker cutover
- cloud database provisioning
- a verified backup/restore process
- deployment secret management
- production migration rollback tooling

Those are separate production-readiness steps.

## How to add the next SQLite migration

1. Add a new `Migration(...)` entry in `backend/app/database_migrations.py`.
2. Use a monotonic id, for example:

```text
0002_add_session_device_metadata
```

3. Write the migration as an idempotent SQLite operation.
4. Add focused tests for fresh and upgraded databases.
5. Update this document and `docs/RELEASE_VALIDATION.md`.

## PostgreSQL migration runner

PostgreSQL schema migrations are defined in `backend/app/postgres_database.py`. The runner acquires a transaction-scoped advisory lock, verifies stored migration checksums, applies pending migrations in order, and records each migration in `schema_migrations` within the same transaction. Unknown newer migrations and changed historical definitions fail closed. The runner applies schema changes only; it does not import or move application data.

The current PostgreSQL migration set includes `pg_0010_workspace_scoped_connectors`, which adds the `agent_connectors` table. It stores provider metadata, workspace/creator identity, status, timestamps, and the validated connector payload. Workspace and creator foreign keys enforce account scope; connector allowed-project membership is checked by `PostgresConnectorRepository`. Connector writes and their audit event commit in the same transaction. This adapter is registered in the lifespan repository bundle, but HTTP connector routes still use SQLite, so this migration does not complete or enable application cutover.

The API does **not** run PostgreSQL migrations automatically at startup. Use the operator CLI from the repository root or `backend/` with `AIXION_POSTGRES_DSN` provided through the environment or a secret manager. The DSN is never accepted as a command-line argument or printed.

Read-only check (default mode):

```bash
cd backend
AIXION_PROFILE=test \
  AIXION_POSTGRES_DSN='host=/path/to/socket port=5432 dbname=aixion_staging user=aixion_migrator' \
  python scripts/migrate_postgres.py --check
```

The check prints applied, missing, unknown, or checksum-mismatched migration IDs. It returns exit code `0` only when the complete expected schema is ready; an empty or inconsistent schema returns `2` and is not modified.

Explicit local apply:

```bash
cd backend
AIXION_PROFILE=test \
  AIXION_POSTGRES_DSN='host=/path/to/socket port=5432 dbname=aixion_test user=aixion_migrator' \
  python scripts/migrate_postgres.py --apply \
    --expected-database aixion_test --expected-schema public
```

For a separately authorized remote staging database, use a DSN with `sslmode=verify-full`, repeat the exact configured database name, and add `--allow-remote`:

```bash
cd backend
AIXION_PROFILE=production \
  AIXION_POSTGRES_DSN='postgresql://USER:PASSWORD@HOST:5432/aixion_staging?sslmode=verify-full' \
  python scripts/migrate_postgres.py --apply \
    --expected-database aixion_staging --expected-schema public --allow-remote
```

The DSN example is a placeholder. Do not put credentials in shell history; inject the value from a protected environment or secret manager. `--apply` is required for any schema mutation, `--expected-database` and `--expected-schema` must exactly match the live PostgreSQL connection, and remote connections require both full TLS verification and the explicit `--allow-remote` flag. Cloud SQL socket paths are treated as remote targets. No cloud target is authorized by this documentation.

Before applying to any shared database, take and verify an independent backup, review the migration set and lock/statement timeouts, and schedule a maintenance window if the migration may lock active tables. PostgreSQL migrations are forward-only; do not delete migration-ledger rows or run ad-hoc reverse DDL to roll back an application release. Use a reviewed forward repair migration or restore a verified backup to a separate recovery target. The current application is not certified for a production migration or deployment.

The runner is covered by `backend/tests/test_postgres_runtime.py`, including check-only behavior on an empty schema, wrong-database refusal before DDL, remote-target and TLS controls, migration application, and idempotent reapplication. Run it with the isolated local PostgreSQL test service:

```bash
cd backend
AIXION_TEST_POSTGRES_DSN='host=/tmp/aixion-ct-pg.<instance>/socket port=55432 dbname=postgres user=<local-user>' \
  python -m pytest tests/test_postgres_runtime.py -q
```

Connector repository, workspace-scope, audit-atomicity, optimistic-update, and runtime bundle coverage:

```bash
cd backend
AIXION_TEST_POSTGRES_DSN='host=/tmp/aixion-ct-pg.<instance>/socket port=55432 dbname=postgres user=<local-user>' \
  python -m pytest tests/test_postgres_connector_repository.py tests/test_postgres_runtime.py -q
```

## Brutal truth

The schema migration runner is now explicit and checksum-verified, but migration safety does not establish application PostgreSQL authority, backup/restore readiness, or production readiness.
