from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditEvent, Idea, Project, ProjectMode, WorkOrder
from app.recovery_routes import RECOVERY_FORMAT_VERSION
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _seed_recovery_data() -> None:
    project = Project(
        name="MCP Shield",
        description="Policy gateway",
        mode=ProjectMode.STRICT,
        rules=["Approval required for mutating tools"],
    )
    idea = Idea(project_id=project.id, title="Recover safely", raw_text="Make backup validation explicit.")
    work_order = WorkOrder(
        project_id=project.id,
        idea_id=idea.id,
        goal="Add recovery proof",
        tasks=["Export operational data", "Validate snapshot shape"],
    )
    audit_event = AuditEvent(
        event_type="recovery.seeded",
        actor="test",
        entity_id=project.id,
        details={"reason": "route test"},
    )
    store.projects[project.id] = project
    store.ideas[idea.id] = idea
    store.work_orders[work_order.id] = work_order
    store.audit_events.append(audit_event)
    store.persist()


def test_recovery_export_returns_safe_operational_snapshot() -> None:
    _seed_recovery_data()

    response = client.get("/audit/recovery/export")

    assert response.status_code == 200
    body = response.json()
    assert body["format_version"] == RECOVERY_FORMAT_VERSION
    assert body["counts"]["projects"] == 1
    assert body["counts"]["ideas"] == 1
    assert body["counts"]["work_orders"] == 1
    assert body["counts"]["audit_events"] == 1
    assert body["entities"]["projects"][0]["name"] == "MCP Shield"
    assert body["entities"]["ideas"][0]["title"] == "Recover safely"
    assert body["excluded_sensitive_collections"] == [
        "users",
        "sessions",
        "invites",
        "external_agents",
        "device_registrations",
    ]
    assert "users" not in body["entities"]
    assert "sessions" not in body["entities"]


def test_recovery_validate_accepts_exported_snapshot_with_warning_for_sensitive_exclusions() -> None:
    _seed_recovery_data()
    snapshot = client.get("/audit/recovery/export").json()

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["counts"]["projects"] == 1
    assert body["warnings"] == [
        "Snapshot excludes auth/session/device/agent secrets and is not a full credential restore artifact."
    ]


def test_recovery_validate_rejects_unknown_entity_collections() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"]["users"] = []

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "Unknown entity collection: users" in body["errors"]


def test_recovery_validate_rejects_invalid_entity_payload() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["entities"]["projects"] = [{"id": "project_missing_required_fields"}]

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any(error.startswith("projects[0] is invalid") for error in body["errors"])


def test_recovery_validate_rejects_unsupported_format_version() -> None:
    snapshot = client.get("/audit/recovery/export").json()
    snapshot["format_version"] = "unsupported"

    response = client.post("/audit/recovery/validate", json={"snapshot": snapshot})

    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "Unsupported recovery format_version: unsupported" in body["errors"]
