from fastapi.testclient import TestClient

from app import main
from app.models import ApprovalStatus, Project
from app.store import SQLiteBackedStore

client = TestClient(main.app)


def setup_function() -> None:
    main.store.reset()


def test_seed_demo_data_creates_mobile_ready_records() -> None:
    response = client.post("/demo/seed")
    assert response.status_code == 200
    body = response.json()
    assert body["projects"] == 2
    assert body["work_orders"] == 1
    assert body["approvals"] == 1
    assert body["test_runs"] == 1
    assert body["notifications"] == 1

    assert client.get("/work-orders").json()[0]["goal"]
    assert client.get("/notifications").json()[0]["status"] == "UNREAD"


def test_api_key_auth_can_block_requests(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_API_KEY", "test-key")

    unauthorized = client.get("/projects")
    assert unauthorized.status_code == 401

    authorized = client.get("/projects", headers={"X-Aixion-Api-Key": "test-key"})
    assert authorized.status_code == 200


def test_sqlite_store_persists_and_reloads(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "aixion.sqlite3"
    monkeypatch.setenv("AIXION_DB_PATH", str(db_path))

    first_store = SQLiteBackedStore()
    first_store.reset()
    project = Project(name="Persistent Project")
    first_store.projects[project.id] = project
    first_store.persist()

    reloaded_store = SQLiteBackedStore()
    assert any(project.name == "Persistent Project" for project in reloaded_store.projects.values())


def test_notifications_are_created_for_approval_requests() -> None:
    seed = client.post("/demo/seed").json()
    assert seed["notifications"] == 1

    notifications = client.get("/notifications").json()
    assert notifications[0]["title"].startswith("Approval needed")

    notification_id = notifications[0]["id"]
    read_response = client.post(f"/notifications/{notification_id}/read")
    assert read_response.status_code == 200
    assert read_response.json()["status"] == "READ"


def test_github_patch_plan_blocks_unapproved_requests() -> None:
    client.post("/demo/seed")
    approval = client.get("/approvals").json()[0]

    response = client.post(
        "/github-runner/patch-plan",
        json={
            "approval_request_id": approval["id"],
            "repository_full_name": "ramgolladi1503-sys/aixion-control-tower",
            "base_branch": "main",
            "feature_branch": "feature/demo",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert "APPROVED" in body["blockers"][0]


def test_github_patch_plan_ready_after_approval() -> None:
    client.post("/demo/seed")
    approval = client.get("/approvals").json()[0]
    main.store.approval_requests[approval["id"]].status = ApprovalStatus.APPROVED
    main.store.approval_requests[approval["id"]].risk.required_actions = []
    main.store.persist()

    response = client.post(
        "/github-runner/patch-plan",
        json={
            "approval_request_id": approval["id"],
            "repository_full_name": "ramgolladi1503-sys/aixion-control-tower",
            "base_branch": "main",
            "feature_branch": "feature/demo",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["files"]
