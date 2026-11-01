from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.models import AuthUser, UserRole


class RecordingApprovalRepository:
    def __init__(self) -> None:
        self.workspace_reads: list[str | None] = []
        self.expiry_workspace_reads: list[str | None] = []

    def list_for_workspace(self, workspace_id: str | None = None) -> list[object]:
        self.workspace_reads.append(workspace_id)
        return []

    def list_due_pending(
        self,
        *,
        now: object,
        legacy_ttl_seconds: int,
        limit: int,
        workspace_id: str | None = None,
    ) -> list[object]:
        del now, legacy_ttl_seconds, limit
        self.expiry_workspace_reads.append(workspace_id)
        return []


@pytest.mark.parametrize(
    "settings",
    [
        SimpleNamespace(is_production=True, postgres_auth_enabled=False),
        SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    ],
)
def test_approval_repository_without_request_fails_closed_for_authoritative_modes(
    monkeypatch,
    settings: SimpleNamespace,
) -> None:
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    with pytest.raises(main.HTTPException) as raised:
        main._approval_repository_for_route(None)

    assert raised.value.status_code == 503
    assert raised.value.headers == {"Retry-After": "1"}


def test_approval_repository_without_request_keeps_local_test_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )

    assert main._approval_repository_for_route(None) is main.approval_decision_repository


def test_approval_list_route_uses_repository_bound_to_fastapi_app(monkeypatch) -> None:
    repository = RecordingApprovalRepository()
    user = AuthUser(
        id="reviewer-test",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.OWNER,
        workspace_id="workspace-test",
    )
    monkeypatch.setattr(
        main.app.state,
        "approval_decision_repository",
        repository,
    )
    monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
    monkeypatch.setattr(main, "_workspace_id_for_user", lambda _user: "workspace-test")
    monkeypatch.setitem(main.app.dependency_overrides, main.require_reviewer, lambda: user)

    with TestClient(main.app) as client:
        response = client.get("/approvals")

    assert response.status_code == 200
    assert response.json() == []
    # Expiration is request-triggered maintenance and must stay within the
    # authenticated workspace, including grouped inbox reads.
    assert repository.workspace_reads == [
        "workspace-test",
        "workspace-test",
    ]
    assert repository.expiry_workspace_reads == [None, "workspace-test"]
