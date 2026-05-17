from __future__ import annotations

import os
from copy import deepcopy

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import Project
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="Connector E2E Demo", description="External agent callback validation")
    store.projects[project.id] = project
    store.persist()
    return project


def _create_connector_from_template(template_id: str, project: Project) -> tuple[dict, dict, str]:
    template_response = client.get(f"/connectors/templates/{template_id}")
    assert template_response.status_code == 200
    template = template_response.json()

    create_payload = deepcopy(template["connector_defaults"])
    create_payload["allowed_project_ids"] = [project.id]
    create_payload["allowed_repositories"] = ["owner/repo"]

    connector_response = client.post("/connectors", json=create_payload)
    assert connector_response.status_code == 200
    connector = connector_response.json()

    mapper = deepcopy(template["mapper"])
    mapper["defaults"] = {
        **mapper.get("defaults", {}),
        "project_id": project.id,
        "requires_approval": True,
    }
    mapper_response = client.put(f"/connectors/{connector['id']}/schema-mapper", json=mapper)
    assert mapper_response.status_code == 200
    assert mapper_response.json()["mapper_enabled"] is True

    secret_response = client.post(f"/connectors/{connector['id']}/secret/issue", json={"note": "e2e validation"})
    assert secret_response.status_code == 200
    secret = secret_response.json()["secret"]

    return template, connector, secret


def _approval_payload(project: Project, task: dict) -> dict:
    return {
        "project_id": project.id,
        "title": f"Approve {task['title']}",
        "summary": "Approval linked from connector-created AgentTask.",
        "agent_name": task["provider"],
        "target_branch": task.get("branch_preference") or "feature/connector-e2e",
        "files": [
            {
                "path": "docs/connector-e2e-demo.md",
                "change_type": "update",
                "diff": "+ connector e2e demo",
                "new_content": "connector e2e demo\n",
            }
        ],
        "test_plan": ["python -m pytest backend/tests/test_connector_e2e_validation_flow.py"],
        "rollback_plan": "Deny the approval or revert the proposed branch.",
    }


def test_chatgpt_connector_payload_reaches_agent_work_approval_and_decision_loop() -> None:
    project = _project()
    template, connector, secret = _create_connector_from_template("chatgpt-actions-bridge", project)

    webhook_response = client.post(
        f"/connectors/{connector['id']}/webhook",
        json=template["sample_payload"],
        headers={"Authorization": f"Bearer {secret}"},
    )

    assert webhook_response.status_code == 200
    webhook_body = webhook_response.json()
    assert webhook_body["accepted"] is True
    assert webhook_body["task_id"]

    task_response = client.get(f"/agent/tasks/{webhook_body['task_id']}")
    assert task_response.status_code == 200
    task = task_response.json()
    assert task["provider"] == "CHATGPT"
    assert task["title"] == template["sample_payload"]["task"]["title"]
    assert task["repository"] == "owner/repo"
    assert task["requires_approval"] is True
    assert task["metadata"]["connector_id"] == connector["id"]
    assert task["metadata"]["connector_webhook"] is True
    assert task["external_agent_id"] == connector["id"]
    assert task["external_agent_name"] == connector["name"]

    listed_tasks = client.get("/agent/tasks", params={"provider": "CHATGPT"})
    assert listed_tasks.status_code == 200
    assert any(item["id"] == task["id"] for item in listed_tasks.json())

    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project, task))
    assert approval_response.status_code == 200
    approval = approval_response.json()
    assert approval["source_provider"] == "CHATGPT"
    assert approval["source_agent_id"] == connector["id"]
    assert approval["source_agent_name"] == connector["name"]
    assert approval["verified_source"] is True

    waiting_task = client.get(f"/agent/tasks/{task['id']}").json()
    assert waiting_task["status"] == "WAITING_FOR_APPROVAL"
    assert waiting_task["approval_request_id"] == approval["id"]

    listed_approvals = client.get("/approvals").json()
    assert any(item["id"] == approval["id"] for item in listed_approvals)

    decision_response = client.post(f"/approvals/{approval['id']}/approve")
    assert decision_response.status_code == 200
    assert decision_response.json()["status"] == "APPROVED"

    approved_task = client.get(f"/agent/tasks/{task['id']}").json()
    assert approved_task["status"] == "APPROVED"

    events_response = client.get(f"/agent/tasks/{task['id']}/events")
    assert events_response.status_code == 200
    event_types = [event["event_type"] for event in events_response.json()]
    assert "TASK_CREATED" in event_types
    assert "APPROVAL_CREATED" in event_types
    assert "APPROVED" in event_types

    audit_types = [event.event_type for event in store.audit_events]
    assert "connector.webhook_task_created" in audit_types
    assert "agent_task.approval_created" in audit_types
    assert "approval.decision" in audit_types
    assert "agent_task.approval_decision_propagated" in audit_types


def test_codex_connector_payload_reaches_agent_work_waiting_approval_state() -> None:
    project = _project()
    template, connector, secret = _create_connector_from_template("codex-agent-bridge", project)

    webhook_response = client.post(
        f"/connectors/{connector['id']}/webhook",
        json=template["sample_payload"],
        headers={"Authorization": f"Bearer {secret}"},
    )

    assert webhook_response.status_code == 200
    task = client.get(f"/agent/tasks/{webhook_response.json()['task_id']}").json()
    assert task["provider"] == "CODEX"
    assert task["title"] == template["sample_payload"]["job"]["title"]
    assert task["repository"] == "owner/repo"
    assert task["branch_preference"] == "feature/codex-task"
    assert task["requires_approval"] is True

    approval_response = client.post(f"/agent/tasks/{task['id']}/approval", json=_approval_payload(project, task))
    assert approval_response.status_code == 200

    waiting_task = client.get(f"/agent/tasks/{task['id']}").json()
    assert waiting_task["status"] == "WAITING_FOR_APPROVAL"
    assert waiting_task["approval_request_id"] == approval_response.json()["id"]
