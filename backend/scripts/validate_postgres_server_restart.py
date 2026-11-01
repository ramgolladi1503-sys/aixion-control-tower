#!/usr/bin/env python3
"""Verify approval delivery claims survive a private local PostgreSQL restart.

The script initializes and removes its own temporary PostgreSQL cluster. It
does not read AIXION_POSTGRES_DSN or connect to a configured application DB.
"""
from __future__ import annotations

import getpass
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Sequence

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.approval_integrity import compute_approval_payload_hash
from app.approval_repository import PostgresApprovalDecisionRepository
from app.models import (
    AgentAuthType,
    AgentProvider,
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    ExternalAgent,
    FileChange,
    Project,
    RiskAssessment,
    RiskLevel,
    Workspace,
)
from app.postgres_database import run_postgres_migrations


def _postgres_bin_dir() -> Path:
    pg_config = shutil.which("pg_config")
    if not pg_config:
        raise RuntimeError("pg_config is required to run the isolated PostgreSQL restart drill")
    result = subprocess.run(
        [pg_config, "--bindir"], check=True, capture_output=True, text=True, timeout=5
    )
    bindir = Path(result.stdout.strip())
    if not (bindir / "initdb").is_file() or not (bindir / "pg_ctl").is_file():
        raise RuntimeError("PostgreSQL initdb and pg_ctl binaries are required")
    return bindir


def _run(command: Sequence[str], *, timeout: int = 30) -> None:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        details = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        raise RuntimeError(f"Local PostgreSQL drill command failed: {Path(command[0]).name}\n{details}")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _seed_and_claim(connect, suffix: str) -> dict[str, str]:
    workspace = Workspace(
        id=f"restart_workspace_{suffix}",
        name="PostgreSQL restart validation",
        owner_user_id=f"restart_user_{suffix}",
    )
    project = Project(
        id=f"restart_project_{suffix}",
        name="PostgreSQL restart validation",
        workspace_id=workspace.id,
    )
    agent = ExternalAgent(
        id=f"restart_agent_{suffix}",
        provider=AgentProvider.ANTIGRAVITY,
        display_name="Local restart drill",
        auth_type=AgentAuthType.API_KEY,
        workspace_id=workspace.id,
    )
    approval = ApprovalRequest(
        id=f"restart_approval_{suffix}",
        project_id=project.id,
        workspace_id=workspace.id,
        title="Persist an approval claim across PostgreSQL restart",
        summary="Synthetic restart validation record.",
        target_branch="validation/restart",
        files=[FileChange(path="README.md", diff="+restart validation")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        source_provider=AgentProvider.ANTIGRAVITY,
        source_agent_id=agent.id,
        source_session_id=f"session:{suffix}",
        source_invocation_id=f"invocation:{suffix}",
        status=ApprovalStatus.APPROVED,
        approved_payload_hash_version=2,
    )
    approval = approval.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approval)}
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
            (workspace.id, Jsonb(workspace.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
            (project.id, workspace.id, Jsonb(project.model_dump(mode="json"))),
        )
        conn.execute(
            "INSERT INTO external_agents "
            "(agent_id,workspace_id,created_by_user_id,provider,auth_type,enabled,secret_hash,payload) "
            "VALUES(%s,%s,NULL,%s,%s,TRUE,NULL,%s)",
            (
                agent.id,
                workspace.id,
                agent.provider.value,
                agent.auth_type.value,
                Jsonb(agent.model_dump(mode="json")),
            ),
        )
        conn.execute(
            "INSERT INTO approval_requests "
            "(approval_id,project_id,workspace_id,source_agent_id,source_invocation_id,status,"
            "expires_at,created_at,updated_at,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                approval.id,
                approval.project_id,
                workspace.id,
                agent.id,
                approval.source_invocation_id,
                approval.status.value,
                approval.expires_at,
                approval.created_at,
                approval.updated_at,
                Jsonb(approval.model_dump(mode="json")),
            ),
        )

    audit = AuditEvent(
        event_type="approval.delivery_claimed",
        actor=f"agent:{agent.id}",
        entity_id=approval.id,
        workspace_id=workspace.id,
        details={"source_invocation_id": approval.source_invocation_id},
    )
    repository = PostgresApprovalDecisionRepository(connect)
    if not repository.claim_approved_delivery(
        approval_id=approval.id,
        workspace_id=workspace.id,
        source_agent_id=agent.id,
        source_invocation_id=approval.source_invocation_id,
        expected_payload_hash=approval.approved_payload_hash,
        audit_event=audit,
    ):
        raise RuntimeError("Initial synthetic approval delivery claim was not committed")
    return {
        "approval_id": approval.id,
        "workspace_id": workspace.id,
        "agent_id": agent.id,
        "invocation_id": approval.source_invocation_id,
        "payload_hash": approval.approved_payload_hash,
    }


def _verify_after_restart(connect, identity: dict[str, str]) -> None:
    repository = PostgresApprovalDecisionRepository(connect)
    approval = repository.get_by_invocation(
        workspace_id=identity["workspace_id"],
        source_agent_id=identity["agent_id"],
        source_invocation_id=identity["invocation_id"],
    )
    if approval is None or approval.id != identity["approval_id"]:
        raise RuntimeError("Approval identity was not recovered after PostgreSQL restart")
    if approval.status != ApprovalStatus.APPROVED:
        raise RuntimeError("Approval status changed across PostgreSQL restart")
    if approval.approved_payload_hash != identity["payload_hash"]:
        raise RuntimeError("Approval payload hash changed across PostgreSQL restart")

    retry_event = AuditEvent(
        event_type="approval.delivery_claimed",
        actor=f"agent:{identity['agent_id']}",
        entity_id=approval.id,
        workspace_id=identity["workspace_id"],
        details={"source_invocation_id": identity["invocation_id"]},
    )
    if repository.claim_approved_delivery(
        approval_id=approval.id,
        workspace_id=identity["workspace_id"],
        source_agent_id=identity["agent_id"],
        source_invocation_id=identity["invocation_id"],
        expected_payload_hash=identity["payload_hash"],
        audit_event=retry_event,
    ):
        raise RuntimeError("Duplicate delivery claim was granted after PostgreSQL restart")

    with connect() as conn:
        claim_count = conn.execute(
            "SELECT count(*) FROM approval_delivery_claims WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT count(*) FROM audit_events "
            "WHERE event_type='approval.delivery_claimed' AND entity_id=%s",
            (approval.id,),
        ).fetchone()[0]
    if claim_count != 1 or audit_count != 1:
        raise RuntimeError("Restart recovery produced an unexpected claim or audit count")


def main() -> int:
    bindir = _postgres_bin_dir()
    with tempfile.TemporaryDirectory(prefix="aixion-pg-restart-") as temporary:
        root = Path(temporary)
        data_dir = root / "data"
        socket_dir = root / "socket"
        socket_dir.mkdir(mode=0o700)
        port = _free_local_port()
        username = getpass.getuser()
        _run(
            [
                str(bindir / "initdb"),
                "-D",
                str(data_dir),
                "--username",
                username,
                "--auth-local=trust",
                "--auth-host=reject",
                "--no-instructions",
            ],
            timeout=60,
        )
        config = data_dir / "postgresql.conf"
        with config.open("a", encoding="utf-8") as stream:
            stream.write(
                "\n# Isolated approval restart validation; no TCP listener.\n"
                "listen_addresses = ''\n"
                f"port = {port}\n"
                f"unix_socket_directories = '{socket_dir}'\n"
            )

        pg_ctl = str(bindir / "pg_ctl")
        server_started = False
        try:
            _run([pg_ctl, "-D", str(data_dir), "start", "-w", "-l", str(root / "postgres.log")])
            server_started = True
            dsn = f"host={socket_dir} port={port} dbname=postgres user={username}"

            def connect():
                return psycopg.connect(dsn)

            run_postgres_migrations(connect)
            suffix = uuid.uuid4().hex
            identity = _seed_and_claim(connect, suffix)
            _run([pg_ctl, "-D", str(data_dir), "stop", "-m", "fast", "-w"])
            server_started = False
            _run([pg_ctl, "-D", str(data_dir), "start", "-w", "-l", str(root / "postgres.log")])
            server_started = True
            _verify_after_restart(connect, identity)
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "postgresql": "isolated temporary local cluster",
                        "tcp_listener": False,
                        "approval_recovered": True,
                        "claim_after_restart": "denied",
                        "durable_claim_rows": 1,
                        "claim_audit_rows": 1,
                        "synthetic_record_ids": identity,
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            if server_started:
                subprocess.run(
                    [pg_ctl, "-D", str(data_dir), "stop", "-m", "fast", "-w"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )


if __name__ == "__main__":
    raise SystemExit(main())
