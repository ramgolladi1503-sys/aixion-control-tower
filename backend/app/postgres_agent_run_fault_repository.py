from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .agent_run_models import AgentRunFaultConfig, AgentRunStepType
from .models import AuditEvent


class PostgresAgentRunFaultRepositoryError(RuntimeError):
    """A PostgreSQL fault record is invalid or inconsistent with its indexed scope."""


class PostgresAgentRunFaultRepository:
    """Workspace-scoped diagnostic fault configuration stored in PostgreSQL."""

    def __init__(self, connect: Callable[[], Connection]) -> None:
        self._connect = connect

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> AgentRunFaultConfig:
        fault_id, workspace_id, run_id, step_type, fault_kind, enabled, uses, payload = row
        try:
            fault = AgentRunFaultConfig.model_validate(payload)
        except Exception as error:
            raise PostgresAgentRunFaultRepositoryError(
                "Stored AgentRun fault payload is invalid"
            ) from error
        if (
            fault.id != fault_id
            or fault.run_id != run_id
            or (fault.step_type.value if fault.step_type is not None else None) != step_type
            or fault.fault_kind.value != fault_kind
            or fault.enabled is not enabled
            or fault.remaining_uses != uses
            or not workspace_id
        ):
            raise PostgresAgentRunFaultRepositoryError(
                "Stored AgentRun fault payload does not match its indexed columns"
            )
        return fault

    @staticmethod
    def _audit(conn: Connection, event: AuditEvent) -> None:
        if not event.workspace_id:
            raise ValueError("PostgreSQL AgentRun fault audits require a workspace")
        conn.execute(
            "INSERT INTO audit_events(event_id,workspace_id,approval_id,entity_type,entity_id,"
            "event_type,actor,details,created_at,payload) "
            "VALUES(%s,%s,NULL,'agent_run_fault',%s,%s,%s,%s,%s,%s)",
            (
                event.id,
                event.workspace_id,
                event.entity_id,
                event.event_type,
                event.actor,
                Jsonb(event.details),
                event.created_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )

    def list_for_workspace(self, workspace_id: str) -> list[AgentRunFaultConfig]:
        if not workspace_id:
            raise ValueError("PostgreSQL AgentRun fault reads require a workspace")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fault_id,workspace_id,run_id,step_type,fault_kind,enabled,"
                "remaining_uses,payload FROM agent_run_faults WHERE workspace_id=%s "
                "ORDER BY created_at DESC,fault_id",
                (workspace_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def create(
        self,
        fault: AgentRunFaultConfig,
        *,
        workspace_id: str,
    ) -> AgentRunFaultConfig | None:
        if not workspace_id or not fault.run_id:
            raise ValueError("PostgreSQL AgentRun fault creation requires workspace and run scope")
        if not fault.enabled or not 1 <= fault.remaining_uses <= 100:
            raise ValueError("PostgreSQL AgentRun fault creation requires a positive use count")
        with self._connect() as conn:
            run = conn.execute(
                "SELECT run_id FROM agent_runs WHERE run_id=%s AND workspace_id=%s FOR KEY SHARE",
                (fault.run_id, workspace_id),
            ).fetchone()
            if run is None:
                return None
            payload = fault.model_dump(mode="json")
            conn.execute(
                "INSERT INTO agent_run_faults(fault_id,workspace_id,run_id,step_type,"
                "fault_kind,enabled,remaining_uses,message,created_by,created_at,payload) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    fault.id,
                    workspace_id,
                    fault.run_id,
                    fault.step_type.value if fault.step_type is not None else None,
                    fault.fault_kind.value,
                    fault.enabled,
                    fault.remaining_uses,
                    fault.message,
                    fault.created_by,
                    fault.created_at,
                    Jsonb(payload),
                ),
            )
            self._audit(
                conn,
                AuditEvent(
                    event_type="agent_run.fault_configured",
                    actor=fault.created_by,
                    entity_id=fault.id,
                    workspace_id=workspace_id,
                    details={
                        "run_id": fault.run_id,
                        "step_type": fault.step_type.value if fault.step_type else None,
                        "fault_kind": fault.fault_kind.value,
                        "remaining_uses": fault.remaining_uses,
                    },
                    created_at=datetime.now(timezone.utc),
                ),
            )
        return fault

    def disable(
        self,
        fault_id: str,
        *,
        workspace_id: str,
        actor: str,
    ) -> AgentRunFaultConfig | None:
        if not workspace_id:
            raise ValueError("PostgreSQL AgentRun fault updates require a workspace")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fault_id,workspace_id,run_id,step_type,fault_kind,enabled,"
                "remaining_uses,payload FROM agent_run_faults "
                "WHERE fault_id=%s AND workspace_id=%s FOR UPDATE",
                (fault_id, workspace_id),
            ).fetchone()
            if row is None:
                return None
            fault = self._decode(row)
            if not fault.enabled:
                return fault
            fault.enabled = False
            conn.execute(
                "UPDATE agent_run_faults SET enabled=FALSE,payload=%s "
                "WHERE fault_id=%s AND workspace_id=%s",
                (Jsonb(fault.model_dump(mode="json")), fault_id, workspace_id),
            )
            self._audit(
                conn,
                AuditEvent(
                    event_type="agent_run.fault_disabled",
                    actor=actor,
                    entity_id=fault.id,
                    workspace_id=workspace_id,
                    details={"fault_kind": fault.fault_kind.value},
                    created_at=datetime.now(timezone.utc),
                ),
            )
        return fault

    def consume(
        self,
        run_id: str,
        step_type: AgentRunStepType,
    ) -> AgentRunFaultConfig | None:
        if not run_id:
            raise ValueError("PostgreSQL AgentRun fault consumption requires a run")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fault_id,workspace_id,run_id,step_type,fault_kind,enabled,"
                "remaining_uses,payload FROM agent_run_faults "
                "WHERE run_id=%s AND enabled=TRUE AND remaining_uses>0 "
                "AND (step_type IS NULL OR step_type=%s) "
                "ORDER BY created_at,fault_id LIMIT 1 FOR UPDATE",
                (run_id, step_type.value),
            ).fetchone()
            if row is None:
                return None
            fault = self._decode(row)
            fault.remaining_uses -= 1
            if fault.remaining_uses == 0:
                fault.enabled = False
            conn.execute(
                "UPDATE agent_run_faults SET enabled=%s,remaining_uses=%s,payload=%s "
                "WHERE fault_id=%s AND workspace_id=%s",
                (
                    fault.enabled,
                    fault.remaining_uses,
                    Jsonb(fault.model_dump(mode="json")),
                    fault.id,
                    row[1],
                ),
            )
            self._audit(
                conn,
                AuditEvent(
                    event_type="agent_run.fault_injected",
                    actor=fault.created_by,
                    entity_id=fault.id,
                    workspace_id=row[1],
                    details={
                        "run_id": run_id,
                        "step_type": step_type.value,
                        "fault_kind": fault.fault_kind.value,
                        "remaining_uses": fault.remaining_uses,
                    },
                    created_at=datetime.now(timezone.utc),
                ),
            )
        return fault
