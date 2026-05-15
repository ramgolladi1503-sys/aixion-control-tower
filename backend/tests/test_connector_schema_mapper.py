from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import Project
from app.store import store

client = TestClient(app)
REPOSITORY = "ramgolladi1503-sys/aixion-control-tower"


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Schema Mapper", description="demo")
    store.projects[project.id] = project
    store.persist()
    return project


def _connector(project_id: str) -> tuple[dict, str]:
    response = client.post(
        "/connectors",
        json={
            "name": "Antigravity Bridge",
            "connector_type": "WEBHOOK",
            "provider_label": "ANTIGRAVITY",
            "endpoint_url": "https://example.com/antigravity",
            "auth_type": "BEARER",
            "allowed_project_ids": [project_id],
            "allowed_repositories": [REPOSITORY],
            "allowed_actions": ["CREATE_AGENT_TASK", "APPEND_AGENT_TASK_EVENT", "READ_AGENT_TASK"],
            "rate_limit_per_minute": 60,
            "enabled": True,
            "config": {"profile": "antigravity"},
        },
    )
    assert response.status_code == 200
    connector = response.json()
    secret_response = client.post(f"/connectors/{connector['id']}/secret/issue")
    assert secret_response.status_code == 200
    return connector, secret_response.json()["secret"]


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _mapper(project_id: str) -> dict:
    return {
        "enabled": True,
        "default_action": "CREATE_AGENT_TASK",
        "field_paths": {
            "title": "task.title",
            "goal": "task.objective",
            "context": "task.notes",
            "repository": "repo.full_name",
            "branch_preference": "repo.branch",
            "metadata": "extra",
        },
        "defaults": {
            "project_id": project_id,
            "requested_action": "GENERATE_DOCS",
            "requires_approval": True,
        },
    }


def test_schema_mapper_can_be_saved_and_previewed() -> None:
    project = _project()
    connector, _ = _connector(project.id)

    saved = client.put(f"/connectors/{connector['id']}/schema-mapper", json=_mapper(project.id))
    preview = client.post(
        f"/connectors/{connector['id']}/schema-mapper/preview",
        json={
            "sample_payload": {
                "task": {"title": "Fix login", "objective": "Normalize foreign payload", "notes": "from agent"},
                "repo": {"full_name": REPOSITORY, "branch": "feature/mapped"},
                "extra": {"source": "antigravity"},
            }
        },
    )

    assert saved.status_code == 200
    assert saved.json()["mapper_enabled"] is True
    assert preview.status_code == 200
    normalized = preview.json()["normalized_payload"]
    assert normalized["action"] == "CREATE_AGENT_TASK"
    assert normalized["project_id"] == project.id
    assert normalized["title"] == "Fix login"
    assert normalized["goal"] == "Normalize foreign payload"
    assert normalized["context"] == "from agent"
    assert normalized["repository"] == REPOSITORY
    assert normalized["branch_preference"] == "feature/mapped"
    assert normalized["metadata"]["source"] == "antigravity"
    assert any(event.event_type == "connector.schema_mapper_updated" for event in store.audit_events)


def test_schema_mapper_normalizes_foreign_payload_before_webhook_task_creation() -> None:
    project = _project()
    connector, secret = _connector(project.id)
    assert client.put(f"/connectors/{connector['id']}/schema-mapper", json=_mapper(project.id)).status_code == 200

    response = client.post(
        f"/connectors/{connector['id']}/webhook",
        json={
            "task": {"title": "Mapped task", "objective": "Create task from foreign shape", "notes": "mapped context"},
            "repo": {"full_name": REPOSITORY, "branch": "feature/mapped-task"},
            "extra": {"source": "foreign-agent"},
        },
        headers=_bearer(secret),
    )

    assert response.status_code == 200
    task = store.agent_tasks[response.json()["task_id"]]
    assert task.title == "Mapped task"
    assert task.goal == "Create task from foreign shape"
    assert task.context == "mapped context"
    assert task.project_id == project.id
    assert task.repository == REPOSITORY
    assert task.branch_preference == "feature/mapped-task"
    assert task.metadata["source"] == "foreign-agent"
    assert task.metadata["connector_webhook"] is True


def test_schema_mapper_can_normalize_event_append_payload() -> None:
    project = _project()
    connector, secret = _connector(project.id)
    created = client.post(
        f"/connectors/{connector['id']}/webhook",
        json={
            "action": "CREATE_AGENT_TASK",
            "project_id": project.id,
            "title": "Owned task",
            "goal": "Create task first",
            "repository": REPOSITORY,
        },
        headers=_bearer(secret),
    ).json()
    mapper = {
        "enabled": True,
        "default_action": "APPEND_AGENT_TASK_EVENT",
        "field_paths": {
            "task_id": "event.task",
            "message": "event.text",
            "event_type": "event.kind",
            "status": "event.state",
            "metadata": "event.extra",
        },
        "defaults": {},
    }
    assert client.put(f"/connectors/{connector['id']}/schema-mapper", json=mapper).status_code == 200

    response = client.post(
        f"/connectors/{connector['id']}/webhook",
        json={"event": {"task": created["task_id"], "text": "Plan ready", "kind": "PLAN_RECEIVED", "state": "PLANNING", "extra": {"artifact": "plan"}}},
        headers=_bearer(secret),
    )

    assert response.status_code == 200
    event = store.agent_task_events[response.json()["event_id"]]
    assert event.message == "Plan ready"
    assert event.event_type == "PLAN_RECEIVED"
    assert event.status == "PLANNING"
    assert event.metadata["artifact"] == "plan"


def test_schema_mapper_rejects_unsupported_target_field() -> None:
    project = _project()
    connector, _ = _connector(project.id)

    response = client.put(
        f"/connectors/{connector['id']}/schema-mapper",
        json={"enabled": True, "default_action": "CREATE_AGENT_TASK", "field_paths": {"secret_hash": "bad.path"}, "defaults": {}},
    )

    assert response.status_code == 422


def test_schema_mapper_rejects_unsafe_path() -> None:
    project = _project()
    connector, _ = _connector(project.id)

    response = client.put(
        f"/connectors/{connector['id']}/schema-mapper",
        json={"enabled": True, "default_action": "CREATE_AGENT_TASK", "field_paths": {"title": "task.__.title"}, "defaults": {}},
    )

    assert response.status_code == 422
