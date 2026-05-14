from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditEvent, now_utc
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _seed_audit_events() -> list[AuditEvent]:
    base_time = now_utc()
    events = [
        AuditEvent(
            event_type="approval.created",
            actor="owner@example.com",
            entity_id="approval_1",
            details={"project_id": "project_1", "risk_level": "HIGH"},
            created_at=base_time - timedelta(minutes=4),
        ),
        AuditEvent(
            event_type="approval.decision",
            actor="owner@example.com",
            entity_id="approval_1",
            details={"decision": "approve", "approved_payload_hash": "sha256:test"},
            created_at=base_time - timedelta(minutes=3),
        ),
        AuditEvent(
            event_type="FORWARDED_AFTER_APPROVAL",
            actor="mcp-gateway",
            entity_id="approval_1",
            details={"mcp_pending_request_id": "mcp_pending_1", "tool_name": "update_config"},
            created_at=base_time - timedelta(minutes=2),
        ),
        AuditEvent(
            event_type="approval.created",
            actor="reviewer@example.com",
            entity_id="approval_2",
            details={"project_id": "project_2", "risk_level": "LOW"},
            created_at=base_time - timedelta(minutes=1),
        ),
    ]
    store.audit_events.extend(events)
    store.persist()
    return events


def test_query_audit_events_filters_by_event_type_entity_actor_and_detail() -> None:
    events = _seed_audit_events()

    by_type = client.get("/audit/events?event_type=FORWARDED_AFTER_APPROVAL")
    assert by_type.status_code == 200
    assert len(by_type.json()) == 1
    assert by_type.json()[0]["id"] == events[2].id

    by_entity = client.get("/audit/events?entity_id=approval_1")
    assert by_entity.status_code == 200
    assert len(by_entity.json()) == 3

    by_actor = client.get("/audit/events?actor=mcp-gateway")
    assert by_actor.status_code == 200
    assert len(by_actor.json()) == 1
    assert by_actor.json()[0]["event_type"] == "FORWARDED_AFTER_APPROVAL"

    by_detail_key = client.get("/audit/events?detail_key=project_id")
    assert by_detail_key.status_code == 200
    assert len(by_detail_key.json()) == 2

    by_detail_value = client.get("/audit/events?detail_key=project_id&detail_value=project_2")
    assert by_detail_value.status_code == 200
    assert len(by_detail_value.json()) == 1
    assert by_detail_value.json()[0]["entity_id"] == "approval_2"


def test_query_audit_events_applies_limit_and_returns_newest_first() -> None:
    events = _seed_audit_events()

    response = client.get("/audit/events?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["id"] == events[-1].id
    assert body[1]["id"] == events[-2].id


def test_export_audit_events_applies_filters_limit_and_retention_policy() -> None:
    events = _seed_audit_events()

    response = client.get("/audit/export?event_type=approval.created&limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["limit"] == 1
    assert body["truncated"] is True
    assert body["filters"] == {"event_type": "approval.created"}
    assert body["retention_policy"]["policy_version"] == "aixion-audit-retention-v1"
    assert body["retention_policy"]["recommended_online_retention_days"] == 365
    assert body["retention_policy"]["deletion_job_enabled"] is False
    assert body["events"][0]["id"] == events[-1].id


def test_export_audit_events_redacts_sensitive_detail_keys() -> None:
    event = AuditEvent(
        event_type="agent.registered",
        actor="owner@example.com",
        entity_id="agent_1",
        details={
            "agent_token": "raw-agent-token",
            "nested": {"api_secret": "raw-secret", "safe": "visible"},
            "safe_value": "kept",
        },
    )
    store.audit_events.append(event)
    store.persist()

    response = client.get("/audit/export")

    assert response.status_code == 200
    exported_event = response.json()["events"][0]
    assert exported_event["details"]["agent_token"] == "[REDACTED]"
    assert exported_event["details"]["nested"]["api_secret"] == "[REDACTED]"
    assert exported_event["details"]["nested"]["safe"] == "visible"
    assert exported_event["details"]["safe_value"] == "kept"


def test_export_audit_events_filters_by_created_window() -> None:
    events = _seed_audit_events()
    created_after = events[1].created_at.isoformat()
    created_before = events[2].created_at.isoformat()

    response = client.get(
        "/audit/export",
        params={"created_after": created_after, "created_before": created_before},
    )

    assert response.status_code == 200
    body = response.json()
    assert [event["id"] for event in body["events"]] == [events[2].id, events[1].id]
    assert body["filters"]["created_after"] == created_after
    assert body["filters"]["created_before"] == created_before


def test_get_audit_event_by_id_and_missing_404() -> None:
    events = _seed_audit_events()

    response = client.get(f"/audit/events/{events[0].id}")
    missing = client.get("/audit/events/audit_missing")

    assert response.status_code == 200
    assert response.json()["event_type"] == "approval.created"
    assert missing.status_code == 404
