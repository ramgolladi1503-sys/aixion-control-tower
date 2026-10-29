from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.postgres_database import POSTGRES_MIGRATIONS, run_postgres_migrations
from app.postgres_runtime import PostgresRuntime


def _configured_local_test_dsn() -> tuple[str, dict[str, str]]:
    """Require an explicit loopback test database before creating scratch DBs."""
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN must name an explicit local disposable database")

    try:
        info = conninfo_to_dict(dsn)
    except (psycopg.Error, ValueError):
        pytest.fail("AIXION_TEST_POSTGRES_DSN is not valid libpq connection information")

    database = info.get("dbname", "").strip()
    if not database.endswith("_test"):
        pytest.skip("backup/restore rehearsal requires a configured database ending in _test")

    # Refuse service aliases and implicit defaults: the target must be explicit
    # enough to establish that this drill is local and disposable.
    if info.get("service"):
        pytest.skip("service-file DSNs are ambiguous for the isolated local restore rehearsal")
    host = info.get("host", "").strip()
    hostaddr = info.get("hostaddr", "").strip()
    local_socket_roots = ("/tmp", "/private/tmp", "/var/run/postgresql", "/run/postgresql")
    if host.startswith("/"):
        is_local = any(host == root or host.startswith(root.rstrip("/") + "/") for root in local_socket_roots)
    elif host:
        try:
            is_local = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_local = False
    else:
        is_local = False
    if hostaddr:
        try:
            is_local = is_local and ipaddress.ip_address(hostaddr).is_loopback
        except ValueError:
            is_local = False
    if not is_local:
        pytest.skip("backup/restore rehearsal accepts only an explicit loopback PostgreSQL target")

    return dsn, info


def _scratch_dsn(base_dsn: str, database: str) -> str:
    return make_conninfo(base_dsn, dbname=database, connect_timeout=5)


def _pg_environment(
    info: dict[str, str], *, database: str, tmp_path: Path
) -> dict[str, str]:
    """Provide libpq settings without exposing credentials in process arguments."""
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("PG"):
            env.pop(key, None)
    env["PGDATABASE"] = database
    env["PGCONNECT_TIMEOUT"] = "5"
    for key, variable in (
        ("host", "PGHOST"),
        ("hostaddr", "PGHOSTADDR"),
        ("port", "PGPORT"),
        ("user", "PGUSER"),
        ("sslmode", "PGSSLMODE"),
        ("sslrootcert", "PGSSLROOTCERT"),
        ("sslcert", "PGSSLCERT"),
        ("sslkey", "PGSSLKEY"),
    ):
        if info.get(key):
            env[variable] = info[key]

    if info.get("passfile"):
        env["PGPASSFILE"] = info["passfile"]
    elif info.get("password"):
        passfile = tmp_path / ".pgpass"

        def escape(value: str) -> str:
            return value.replace("\\", "\\\\").replace(":", "\\:").replace("\n", "")

        fields = (
            info.get("host", "*"),
            info.get("port", "*"),
            "*",
            info.get("user", "*"),
            info["password"],
        )
        passfile.write_text(":".join(escape(value) for value in fields) + "\n")
        passfile.chmod(0o600)
        env["PGPASSFILE"] = str(passfile)
    return env


def _run_pg_tool(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_local_postgres_logical_backup_restores_data_constraints_and_readiness(
    tmp_path: Path,
) -> None:
    """Exercise the documented custom dump/restore against newly created DBs only."""
    base_dsn, info = _configured_local_test_dsn()
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        pytest.skip("pg_dump and pg_restore must be installed for the backup/restore rehearsal")

    tmp_path.chmod(0o700)
    archive = tmp_path / "synthetic-postgres-rehearsal.dump"
    suffix = uuid.uuid4().hex
    source_name = f"aixion_restore_src_{suffix}_test"
    recovery_name = f"aixion_restore_dst_{suffix}_test"
    created_databases: list[str] = []

    with psycopg.connect(base_dsn, autocommit=True, connect_timeout=5) as admin:
        configured_database = info.get("dbname", "")
        actual_database = admin.execute("SELECT current_database()").fetchone()[0]
        assert actual_database == configured_database, "connected DB differs from explicit test DSN"

        try:
            # Each CREATE is recorded only after PostgreSQL confirms success;
            # cleanup can therefore touch only databases created by this test.
            for database in (source_name, recovery_name):
                existing = admin.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
                ).fetchone()
                assert existing is None, "UUID scratch database unexpectedly already exists"
                try:
                    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
                except psycopg.errors.InsufficientPrivilege:
                    pytest.skip("configured local test role cannot create isolated scratch databases")
                created_databases.append(database)

            source_dsn = _scratch_dsn(base_dsn, source_name)
            recovery_dsn = _scratch_dsn(base_dsn, recovery_name)

            def source_connect() -> psycopg.Connection:
                return psycopg.connect(source_dsn)

            ledger = run_postgres_migrations(source_connect)
            expected_ids = sorted(migration.migration_id for migration in POSTGRES_MIGRATIONS)
            assert [row["migration_id"] for row in ledger] == expected_ids

            workspace_id = f"restore-workspace-{suffix}"
            owner_user_id = f"restore-owner-{suffix}"
            project_id = f"restore-project-{suffix}"
            workspace_payload = {
                "id": workspace_id,
                "name": "Synthetic backup restore rehearsal",
                "owner_user_id": owner_user_id,
                "fixture": "postgres-logical-restore",
            }
            project_payload = {
                "id": project_id,
                "workspace_id": workspace_id,
                "name": "Synthetic restored project",
                "metadata": {"fixture": "only-synthetic-data", "revision": 1},
            }
            with source_connect() as source:
                source.execute(
                    "INSERT INTO workspaces (workspace_id, payload) VALUES (%s, %s)",
                    (workspace_id, psycopg.types.json.Jsonb(workspace_payload)),
                )
                source.execute(
                    "INSERT INTO projects (project_id, workspace_id, payload) VALUES (%s, %s, %s)",
                    (project_id, workspace_id, psycopg.types.json.Jsonb(project_payload)),
                )

            dump_env = _pg_environment(info, database=source_name, tmp_path=tmp_path)
            dumped = _run_pg_tool(
                [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--file={archive}",
                ],
                env=dump_env,
            )
            assert dumped.returncode == 0, "pg_dump failed for the isolated synthetic source database"
            archive.chmod(0o600)
            assert archive.is_file() and archive.stat().st_size > 0

            list_env = _pg_environment(info, database=recovery_name, tmp_path=tmp_path)
            listed = _run_pg_tool([pg_restore, "--list", str(archive)], env=list_env)
            assert listed.returncode == 0, "pg_restore could not read the custom-format archive"
            restored = _run_pg_tool(
                [
                    pg_restore,
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                    "--no-privileges",
                    f"--dbname={recovery_name}",
                    str(archive),
                ],
                env=list_env,
            )
            assert restored.returncode == 0, "pg_restore failed for the fresh recovery database"

            runtime = PostgresRuntime(
                _dsn=recovery_dsn,
                profile="test",
                _connector=psycopg.connect,
            )
            probe = runtime.probe()
            assert probe.ready, f"restored PostgreSQL runtime is not ready: {probe.schema}"
            assert probe.schema.applied_migration_ids == tuple(expected_ids)
            assert not probe.schema.missing_migration_ids
            assert not probe.schema.unknown_migration_ids
            assert not probe.schema.mismatched_migration_ids

            with psycopg.connect(recovery_dsn) as recovery:
                persisted_ledger = recovery.execute(
                    "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
                ).fetchall()
                assert [row[0] for row in persisted_ledger] == expected_ids
                restored_workspace = recovery.execute(
                    "SELECT payload FROM workspaces WHERE workspace_id=%s", (workspace_id,)
                ).fetchone()
                assert restored_workspace == (workspace_payload,)
                restored_project = recovery.execute(
                    "SELECT workspace_id, payload FROM projects WHERE project_id=%s", (project_id,)
                ).fetchone()
                assert restored_project == (workspace_id, project_payload)

                with pytest.raises(psycopg.errors.ForeignKeyViolation):
                    with recovery.transaction():
                        recovery.execute(
                            "INSERT INTO projects (project_id, workspace_id, payload) "
                            "VALUES (%s, %s, %s)",
                            (
                                f"orphan-project-{suffix}",
                                f"missing-workspace-{suffix}",
                                psycopg.types.json.Jsonb(
                                    {
                                        "id": f"orphan-project-{suffix}",
                                        "workspace_id": f"missing-workspace-{suffix}",
                                    }
                                ),
                            ),
                        )

            # Confirm the dump/restore did not mutate or consume the source.
            with source_connect() as source:
                assert source.execute(
                    "SELECT payload FROM workspaces WHERE workspace_id=%s", (workspace_id,)
                ).fetchone() == (workspace_payload,)
                assert source.execute(
                    "SELECT workspace_id, payload FROM projects WHERE project_id=%s", (project_id,)
                ).fetchone() == (workspace_id, project_payload)
        finally:
            for database in reversed(created_databases):
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(database)
                    )
                )
