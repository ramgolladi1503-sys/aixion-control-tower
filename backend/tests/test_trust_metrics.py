from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import AuthUser, UserRole
from app.postgres_trust_repository import PostgresTrustMetricsSnapshot
from app.trust_metrics import prometheus_trust_metrics
from app.trust_metrics_routes import get_trust_metrics
from app.trust_models import FlightRecorderVerification


def _user() -> AuthUser:
    return AuthUser(
        id="maintainer-1",
        email="maintainer@example.test",
        display_name="Maintainer",
        role=UserRole.MAINTAINER,
        workspace_id="workspace-a",
        email_verified=True,
    )


def _snapshot() -> PostgresTrustMetricsSnapshot:
    return PostgresTrustMetricsSnapshot(
        lease_status_counts={"ACTIVE": 2},
        policy_decision_counts={"ALLOW": 1},
        provider_decision_counts={("CODEX", "ALLOW"): 1},
        authorization_count=1,
        consumption_count=1,
        active_credential_count=1,
        revoked_credential_count=0,
        trust_event_count=3,
        push_status_counts={"SENT": 1},
        push_attempt_count=1,
        push_queue_depth=0,
        verification=FlightRecorderVerification(valid=True, event_count=3),
    )


class _MembershipRepository:
    def __init__(self, workspace_id: str | None) -> None:
        self.workspace_id = workspace_id
        self.calls: list[tuple[str, str | None]] = []

    def snapshot(self, *, user_id: str, claimed_workspace_id: str | None):
        self.calls.append((user_id, claimed_workspace_id))
        if self.workspace_id is None:
            return SimpleNamespace(
                user=None,
                workspace_count=0,
                resolved_workspace_exists=False,
            )
        return SimpleNamespace(
            user=SimpleNamespace(workspace_id=self.workspace_id),
            workspace_count=1,
            resolved_workspace_exists=True,
        )


class _FailingLegacyMembershipRepository:
    def snapshot(self, **_kwargs):
        pytest.fail("PostgreSQL metrics must not consult the global membership repository")


def _postgres_metrics_request(membership_repository: _MembershipRepository):
    connection = SimpleNamespace(
        execute=lambda _query: None,
        transaction=lambda: nullcontext(),
    )
    observed: dict[str, object] = {}
    snapshot = _snapshot()

    class TrustRepository:
        def metrics_snapshot(self, conn, *, workspace_id, now, trust_event_repository):
            observed.update(
                conn=conn,
                workspace_id=workspace_id,
                now=now,
                trust_event_repository=trust_event_repository,
            )
            return snapshot

    repositories = SimpleNamespace(
        trust=TrustRepository(),
        trust_events=object(),
        approval_decisions=object(),
        workspace_membership=membership_repository,
        projects=object(),
        notification_outbox=object(),
    )
    runtime = SimpleNamespace(
        repositories=repositories,
        connect=lambda: nullcontext(connection),
    )
    state = SimpleNamespace(
        postgres_runtime=runtime,
        postgres_repositories=repositories,
        approval_decision_repository=repositories.approval_decisions,
        workspace_membership_repository=repositories.workspace_membership,
        trust_repository=repositories.trust,
        trust_event_repository=repositories.trust_events,
        project_repository=repositories.projects,
        notification_outbox_repository=repositories.notification_outbox,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return request, observed, repositories, connection, snapshot


def test_postgres_snapshot_renderer_preserves_metric_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = PostgresTrustMetricsSnapshot(
        lease_status_counts={"ACTIVE": 2},
        policy_decision_counts={"ALLOW": 1},
        provider_decision_counts={("CODEX", "ALLOW"): 1},
        authorization_count=1,
        consumption_count=1,
        active_credential_count=1,
        revoked_credential_count=0,
        trust_event_count=3,
        push_status_counts={"SENT": 1},
        push_attempt_count=2,
        push_queue_depth=0,
        verification=FlightRecorderVerification(valid=True, event_count=3),
    )
    monkeypatch.setattr(
        "app.trust_metrics.verify_flight_recorder",
        lambda: pytest.fail("PostgreSQL snapshots must not read the SQLite ledger"),
    )

    output = prometheus_trust_metrics("workspace-a", postgres_snapshot=snapshot)

    assert 'aixion_capability_leases_total{status="ACTIVE"} 2' in output
    assert 'aixion_policy_decisions_by_provider_total{decision="ALLOW",provider="CODEX"} 1' in output
    assert "aixion_action_authorizations_total 1" in output
    assert "aixion_action_consumptions_total 1" in output
    assert "aixion_credential_grants_active 1" in output
    assert "aixion_trust_flight_recorder_valid 1" in output
    assert "aixion_trust_flight_recorder_events_total 3" in output
    assert 'aixion_push_notifications_total{status="SENT"} 1' in output
    assert "aixion_push_dispatch_attempts_total 2" in output
    assert "aixion_push_retry_queue_depth 0" in output


def test_trust_metrics_refuses_partial_postgres_binding_without_sqlite_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = SimpleNamespace(trust=object(), trust_events=object())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_runtime=object(),
                postgres_repositories=repositories,
                trust_repository=repositories.trust,
            )
        )
    )
    monkeypatch.setattr(
        "app.trust_metrics_routes.prometheus_trust_metrics",
        lambda *_args, **_kwargs: pytest.fail("must not fall back to SQLite metrics"),
    )

    with pytest.raises(HTTPException) as error:
        get_trust_metrics(request, _user())

    assert error.value.status_code == 503


def test_trust_metrics_uses_the_app_bound_postgres_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership_repository = _MembershipRepository("workspace-a")
    request, observed, repositories, connection, _snapshot_value = (
        _postgres_metrics_request(membership_repository)
    )
    monkeypatch.setattr(
        "app.workspace_access.workspace_membership_repository",
        _FailingLegacyMembershipRepository(),
    )
    monkeypatch.setattr(
        "app.trust_metrics_routes.prometheus_trust_metrics",
        lambda workspace_id, *, postgres_snapshot: (
            f"workspace={workspace_id};active={postgres_snapshot.lease_status_counts['ACTIVE']}\n"
        ),
    )

    response = get_trust_metrics(request, _user())

    assert response.status_code == 200
    assert response.body == b"workspace=workspace-a;active=2\n"
    assert membership_repository.calls == [("maintainer-1", "workspace-a")]
    assert observed["workspace_id"] == "workspace-a"
    assert observed["conn"] is connection
    assert observed["trust_event_repository"] is repositories.trust_events


def test_trust_metrics_requires_membership_from_the_app_bound_postgres_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    membership_repository = _MembershipRepository(None)
    request, observed, _repositories, _connection, _snapshot_value = (
        _postgres_metrics_request(membership_repository)
    )
    monkeypatch.setattr(
        "app.workspace_access.workspace_membership_repository",
        _FailingLegacyMembershipRepository(),
    )
    monkeypatch.setattr(
        "app.trust_metrics_routes.prometheus_trust_metrics",
        lambda *_args, **_kwargs: pytest.fail("must not render metrics without membership"),
    )

    with pytest.raises(HTTPException) as error:
        get_trust_metrics(request, _user())

    assert error.value.status_code == 403
    assert membership_repository.calls == [("maintainer-1", "workspace-a")]
    assert "workspace_id" not in observed
