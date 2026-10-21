from fastapi.testclient import TestClient

from app import main
from app.models import AuthUser, UserRole


class RecordingAgentTaskRepository:
    def __init__(self) -> None:
        self.list_arguments: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> list[object]:
        self.list_arguments.append(kwargs)
        return []


def test_agent_task_list_route_uses_repository_bound_to_fastapi_app(monkeypatch) -> None:
    repository = RecordingAgentTaskRepository()
    user = AuthUser(
        id="reviewer-test",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.OWNER,
        workspace_id="workspace-test",
    )
    monkeypatch.setattr(main.app.state, "agent_task_repository", repository)
    monkeypatch.setattr(main, "reconcile_linked_approval_tasks", lambda **_kwargs: 0)
    monkeypatch.setitem(main.app.dependency_overrides, main.require_reviewer, lambda: user)

    try:
        with TestClient(main.app) as client:
            response = client.get("/agent/tasks")
    finally:
        main.app.dependency_overrides.pop(main.require_reviewer, None)

    assert response.status_code == 200
    assert response.json() == []
    assert repository.list_arguments == [
        {
            "workspace_id": "workspace-test",
            "provider": None,
            "status": None,
            "project_id": None,
            "limit": 100,
        }
    ]
