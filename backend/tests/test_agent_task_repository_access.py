from fastapi.testclient import TestClient

from app import main
from app.agent_task_repository import SQLiteAgentTaskRepository
from app.auth import create_user
from app.models import AuthUser, UserRole
from app.store import store


class RecordingAgentTaskRepository(SQLiteAgentTaskRepository):
    def __init__(self) -> None:
        super().__init__(store._connect)
        self.list_arguments: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> list[object]:
        self.list_arguments.append(kwargs)
        return []


def test_agent_task_list_route_uses_repository_bound_to_fastapi_app(monkeypatch) -> None:
    # This test exercises the explicit local repository binding. CI may run
    # the suite with production/PostgreSQL-auth environment defaults, where a
    # request must use the lifespan-owned PostgreSQL repository bundle instead.
    monkeypatch.setenv("AIXION_PROFILE", "local")
    monkeypatch.setenv("AIXION_POSTGRES_AUTH_ENABLED", "false")
    store.reset()
    repository = RecordingAgentTaskRepository()
    stored_user = create_user(
        "reviewer@example.test",
        "valid-password-123",
        "Reviewer",
        role=UserRole.OWNER,
        create_workspace=True,
    )
    user = AuthUser(
        id=stored_user.id,
        email=stored_user.email,
        display_name=stored_user.display_name,
        role=stored_user.role,
        workspace_id=stored_user.workspace_id,
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
            "workspace_id": stored_user.workspace_id,
            "provider": None,
            "status": None,
            "project_id": None,
            "limit": 100,
        }
    ]
