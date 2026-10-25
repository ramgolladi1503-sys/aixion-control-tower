from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditEvent, now_utc
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()
    app.state.postgres_runtime = None


def _normalize_utc_text(value: str) -> str:
    return value.replace("Z", "+00:00")


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


def test_audit_routes_read_durable_repository_when_process_cache_is_empty() -> None:
    events = _seed_audit_events()
    store.audit_events.clear()

    response = client.get("/audit/events?entity_id=approval_1")
    detail = client.get(f"/audit/events/{events[0].id}")
    exported = client.get("/audit/export?entity_id=approval_1")

    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {event.id for event in events[:3]}
    assert detail.status_code == 200
    assert detail.json()["id"] == events[0].id
    assert exported.status_code == 200
    assert exported.json()["count"] == 3


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


def test_audit_read_endpoints_redact_sensitive_detail_keys() -> None:
    event = AuditEvent(
        event_type="agent.registered",
        actor="owner@example.com",
        entity_id="agent_sensitive_read",
        details={
            "agent_token": "raw-agent-token",
            "nested": [{"api_secret": "raw-secret", "safe": "visible"}],
            "safe_value": "kept",
        },
    )
    store.audit_events.append(event)
    store.persist()

    list_response = client.get("/audit/events?entity_id=agent_sensitive_read")
    detail_response = client.get(f"/audit/events/{event.id}")

    assert list_response.status_code == 200
    listed_event = list_response.json()[0]
    assert listed_event["details"]["agent_token"] == "[REDACTED]"
    assert listed_event["details"]["nested"][0]["api_secret"] == "[REDACTED]"
    assert listed_event["details"]["nested"][0]["safe"] == "visible"
    assert listed_event["details"]["safe_value"] == "kept"

    assert detail_response.status_code == 200
    detailed_event = detail_response.json()
    assert detailed_event["details"]["agent_token"] == "[REDACTED]"
    assert detailed_event["details"]["nested"][0]["api_secret"] == "[REDACTED]"
    assert detailed_event["details"]["nested"][0]["safe"] == "visible"
    assert detailed_event["details"]["safe_value"] == "kept"


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
    assert _normalize_utc_text(body["filters"]["created_after"]) == created_after
    assert _normalize_utc_text(body["filters"]["created_before"]) == created_before


def test_get_audit_event_by_id_and_missing_404() -> None:
    events = _seed_audit_events()

    response = client.get(f"/audit/events/{events[0].id}")
    missing = client.get("/audit/events/audit_missing")

    assert response.status_code == 200
    assert response.json()["event_type"] == "approval.created"
    assert missing.status_code == 404


def test_audit_and_recovery_reads_fail_closed_when_postgres_runtime_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state, "postgres_runtime", SimpleNamespace(), raising=False)
    from app.recovery_routes import recovery_snapshot_repository

    def unexpected_snapshot_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite recovery snapshot must not be read in PostgreSQL mode")

    monkeypatch.setattr(recovery_snapshot_repository, "read_snapshot", unexpected_snapshot_read)

    responses = [
        client.get("/audit/events"),
        client.get("/audit/export"),
        client.get("/audit/events/nonexistent"),
        client.get("/audit/recovery/export"),
        client.post("/audit/recovery/validate", json={"snapshot": {}}),
    ]

    assert [response.status_code for response in responses] == [503] * 5
    assert all("unified audit writer" in response.json()["detail"] for response in responses)


def test_root_audit_read_fails_closed_instead_of_serving_sqlite_during_postgres_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _seed_audit_events()
    monkeypatch.setattr(app.state, "postgres_runtime", SimpleNamespace(), raising=False)

    response = client.get("/audit")

    assert response.status_code == 503
    assert "unified audit writer" in response.json()["detail"]
    assert len(store.audit_events) == len(events)


def test_audit_and_recovery_reads_fail_closed_when_postgres_auth_is_selected_without_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import audit_routes, main
    from app import recovery_routes
    from app.recovery_routes import recovery_snapshot_repository

    settings = SimpleNamespace(postgres_auth_enabled=True)
    monkeypatch.setattr(audit_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(recovery_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(app.state, "postgres_runtime", None, raising=False)

    def unexpected_audit_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite audit repository must not be read in PostgreSQL auth mode")

    def unexpected_snapshot_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite recovery snapshot must not be read in PostgreSQL auth mode")

    monkeypatch.setattr(audit_routes._local_audit_repository, "list_for_workspace", unexpected_audit_read)
    monkeypatch.setattr(audit_routes._local_audit_repository, "get_for_workspace", unexpected_audit_read)
    monkeypatch.setattr(recovery_snapshot_repository, "read_snapshot", unexpected_snapshot_read)

    responses = [
        client.get("/audit"),
        client.get("/audit/events"),
        client.get("/audit/export"),
        client.get("/audit/events/nonexistent"),
        client.get("/audit/recovery/export"),
        client.post("/audit/recovery/validate", json={"snapshot": {}}),
    ]

    assert [response.status_code for response in responses] == [503] * len(responses)
    assert "authoritative recovery repository" in responses[4].json()["detail"]
    assert "unified audit writer" in responses[5].json()["detail"]


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
    ids=["production", "postgres-auth"],
)
def test_audit_route_rejects_missing_request_context_without_sqlite_fallback(
    monkeypatch: pytest.MonkeyPatch, settings: SimpleNamespace
) -> None:
    from fastapi import HTTPException

    from app import audit_routes

    monkeypatch.setattr(audit_routes, "get_settings", lambda: settings)

    def unexpected_audit_read(*args: object, **kwargs: object) -> object:
        raise AssertionError("SQLite audit repository must not be read without request context")

    monkeypatch.setattr(
        audit_routes._local_audit_repository,
        "list_for_workspace",
        unexpected_audit_read,
    )
    monkeypatch.setattr(
        audit_routes._local_audit_repository,
        "get_for_workspace",
        unexpected_audit_read,
    )

    with pytest.raises(HTTPException) as raised:
        audit_routes.query_audit_events(request=None, user=None)  # type: ignore[arg-type]

    assert raised.value.status_code == 503
    assert "request context is required" in raised.value.detail


def test_audit_repository_keeps_local_direct_behavior_without_production_or_postgres_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import audit_routes

    monkeypatch.setattr(
        audit_routes,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )

    assert audit_routes._audit_repository_for_route(None) is audit_routes._local_audit_repository
