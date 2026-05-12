# Release Validation Addendum: Database Migrations

Use this addendum with `docs/RELEASE_VALIDATION.md` for PR #66 database migration validation.

## Required command

```bash
cd backend
python -m pytest tests/test_database_migrations.py
```

## Required proof

The focused migration test must prove:

```text
fresh SQLite database receives baseline migration metadata
migration runner can run repeatedly without duplicating rows
existing persisted key-value state survives startup migration
unknown newer migration fails safely
migration metadata persists across connections
```

## Migration files

```text
backend/app/database_migrations.py
backend/tests/test_database_migrations.py
docs/DATABASE_MIGRATIONS.md
```

## Current migration

```text
0001_baseline_kv_store
```

Expected behavior:

```text
creates schema_migrations table
creates kv_store table if missing
records migration_id, name, and applied_at
preserves existing kv_store rows
```

## Go / no-go rule

Do not move to production environment and configuration hardening until this passes:

```text
python -m pytest tests/test_database_migrations.py
```

Safe claim:

```text
Backend SQLite persistence now has a migration/versioning foundation.
```

Unsafe claim:

```text
Production database, backup, and restore discipline are complete.
```

## Known limitation

The full release checklist remains the main demo gate. This addendum adds the PR #66 database-migration gate as a focused validation note.
