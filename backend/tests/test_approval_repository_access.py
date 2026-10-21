from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app.models import AuthUser, UserRole


class RecordingApprovalRepository:
    def __init__(self) -> None:
        self.workspace_reads: list[str | None] = []

    def list_for_workspace(self, workspace_id: str | None = None) -> list[object]:
        self.workspace_reads.append(workspace_id)
        return []


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
    assert repository.workspace_reads == ["workspace-test", None, "workspace-test"]
