# Backend Database Migrations

Aixion Control Tower now has a small migration/versioning foundation for the backend SQLite store.

This is not PostgreSQL. It is not production backup/restore. It is the minimum disciplined layer needed before deployment and secrets work.

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

## What this does not solve

This PR does not add:

```text
PostgreSQL
Alembic
cloud database provisioning
backup and restore process
deployment secrets
encrypted database storage
row-level schema normalization
production migration rollback tooling
```

Those are later production-readiness steps.

## How to add the next migration

1. Add a new `Migration(...)` entry in `backend/app/database_migrations.py`.
2. Use a monotonic id, for example:

```text
0002_add_session_device_metadata
```

3. Write the migration as an idempotent SQLite operation.
4. Add focused tests for fresh and upgraded databases.
5. Update this document and `docs/RELEASE_VALIDATION.md`.

## Brutal truth

Before this, the SQLite store was persistent but not versioned. That is fine for a demo but sloppy for any real product path. This foundation gives the backend a safe upgrade spine without pretending the storage layer is production-grade yet.
