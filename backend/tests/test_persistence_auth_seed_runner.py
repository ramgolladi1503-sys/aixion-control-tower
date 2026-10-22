import os
import stat
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
from fastapi.testclient import TestClient

from app import main
from app.models import Project
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


def test_bearer_auth_can_block_requests(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")

    unauthorized = client.get("/projects")
    assert unauthorized.status_code == 401

    register = client.post(
        "/auth/register",
        json={
            "email": "tester@example.com",
            "password": "StrongPass123!",
            "display_name": "Tester",
        },
    )
    assert register.status_code == 200

    verification_code = register.json()["dev_verification_code"]
    verify = client.post(
        "/auth/verify-email",
        json={"email": "tester@example.com", "code": verification_code},
    )
    assert verify.status_code == 200

    login = client.post(
        "/auth/login",
        json={"email": "tester@example.com", "password": "StrongPass123!"},
    )
    assert login.status_code == 200

    token = login.json()["access_token"]
    authorized = client.get("/projects", headers={"Authorization": f"Bearer {token}"})
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
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_sqlite_store_restricts_existing_database_sidecars(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "private.sqlite3"
    monkeypatch.setenv("AIXION_DB_PATH", str(db_path))
    local_store = SQLiteBackedStore()
    sidecar = db_path.with_name(db_path.name + "-wal")
    sidecar.write_text("test journal marker")
    sidecar.chmod(0o644)

    local_store._secure_database_files(create_main=False)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_sqlite_store_tolerates_journal_removed_between_existence_and_stat(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    db_path = tmp_path / "racing.sqlite3"
    monkeypatch.setenv("AIXION_DB_PATH", str(db_path))
    local_store = SQLiteBackedStore()
    journal_path = db_path.with_name(db_path.name + "-journal")
    journal_path.write_text("transient journal")
    journal_path.chmod(0o644)

    original_lstat = Path.lstat
    removed_race = False

    def lstat_with_journal_race(path: Path):
        nonlocal removed_race
        if path == journal_path and not removed_race:
            removed_race = True
            journal_path.unlink()
            raise FileNotFoundError(path)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat_with_journal_race)

    local_store._secure_database_files(create_main=False)

    assert removed_race
    assert stat.S_IMODE(original_lstat(db_path).st_mode) == 0o600


def test_sqlite_store_rejects_symlink_database_path(tmp_path, monkeypatch) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"")
    database_link = tmp_path / "linked.sqlite3"
    database_link.symlink_to(target)
    monkeypatch.setenv("AIXION_DB_PATH", str(database_link))

    try:
        SQLiteBackedStore()
    except PermissionError as exc:
        assert "regular files" in str(exc)
    else:
        raise AssertionError("SQLite store accepted a symlinked database path")


def test_sqlite_store_rejects_world_writable_non_sticky_directory(tmp_path, monkeypatch) -> None:
    unsafe_directory = tmp_path / "unsafe"
    unsafe_directory.mkdir()
    unsafe_directory.chmod(0o777)
    monkeypatch.setenv("AIXION_DB_PATH", str(unsafe_directory / "data.sqlite3"))

    try:
        SQLiteBackedStore()
    except PermissionError as exc:
        assert "directory" in str(exc)
    else:
        raise AssertionError("SQLite store accepted a non-sticky world-writable directory")


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
    request = main.store.approval_requests[approval["id"]]
    request.risk.required_actions = []
    main.store.persist()

    decision = client.post(
        f"/approvals/{approval['id']}/decision",
        json={"decision": "approve", "reason": "Seeded GitHub runner validation"},
    )
    assert decision.status_code == 200
    assert decision.json()["approved_payload_hash"]

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
