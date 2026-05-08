from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def test_idea_to_approval_flow_allows_low_risk_docs_change() -> None:
    project_response = client.post(
        "/projects",
        json={"name": "MCP Shield", "description": "Security gateway MVP"},
    )
    assert project_response.status_code == 200
    project_id = project_response.json()["id"]

    idea_response = client.post(
        "/ideas",
        json={
            "project_id": project_id,
            "title": "Improve README",
            "raw_text": "Add architecture section to README.",
        },
    )
    assert idea_response.status_code == 200
    idea_id = idea_response.json()["id"]

    work_response = client.post(
        "/work-orders",
        json={
            "project_id": project_id,
            "idea_id": idea_id,
            "goal": "Document the MVP architecture",
            "tasks": ["Update README", "Add docs link"],
            "affected_areas": ["docs"],
            "required_tests": [],
            "rollback_plan": "Revert README commit",
        },
    )
    assert work_response.status_code == 200
    work_order_id = work_response.json()["id"]

    approval_response = client.post(
        "/approvals",
        json={
            "project_id": project_id,
            "work_order_id": work_order_id,
            "title": "Update README architecture section",
            "summary": "Adds architecture overview to documentation.",
            "agent_name": "builder-agent",
            "target_branch": "feature/readme-architecture",
            "files": [
                {
                    "path": "docs/ARCHITECTURE.md",
                    "change_type": "update",
                    "diff": "+ Add architecture section",
                }
            ],
            "test_plan": [],
            "rollback_plan": "Revert documentation commit",
        },
    )
    assert approval_response.status_code == 200
    approval = approval_response.json()
    assert approval["status"] == "PENDING_REVIEW"
    assert approval["risk"]["level"] == "LOW"

    decision_response = client.post(
        f"/approvals/{approval['id']}/decision",
        json={"decision": "approve", "reason": "Docs-only change"},
    )
    assert decision_response.status_code == 200
    assert decision_response.json()["status"] == "APPROVED"

    audit_response = client.get("/audit")
    assert audit_response.status_code == 200
    assert len(audit_response.json()) >= 4


def test_blocks_protected_branch_request() -> None:
    project_id = client.post("/projects", json={"name": "Tradebot"}).json()["id"]

    response = client.post(
        "/approvals",
        json={
            "project_id": project_id,
            "title": "Unsafe main edit",
            "summary": "Attempts direct main edit.",
            "target_branch": "main",
            "files": [
                {
                    "path": "core/execution_engine.py",
                    "change_type": "update",
                    "diff": "+ change execution",
                }
            ],
            "test_plan": ["pytest"],
            "rollback_plan": "revert commit",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "BLOCKED"
    assert body["risk"]["level"] == "BLOCKED"

    decision = client.post(
        f"/approvals/{body['id']}/decision",
        json={"decision": "approve", "reason": "try anyway"},
    )
    assert decision.status_code == 409


def test_high_risk_request_requires_test_and_rollback_plan() -> None:
    project_id = client.post("/projects", json={"name": "Tradebot"}).json()["id"]

    response = client.post(
        "/approvals",
        json={
            "project_id": project_id,
            "title": "Execution gate update",
            "summary": "Modifies execution gate logic.",
            "target_branch": "feature/execution-gate",
            "files": [
                {
                    "path": "core/execution_gate.py",
                    "change_type": "update",
                    "diff": "+ block stale LTP",
                }
            ],
            "test_plan": [],
            "rollback_plan": "",
        },
    )
    assert response.status_code == 200
    approval = response.json()
    assert approval["risk"]["level"] == "CRITICAL"
    assert approval["risk"]["required_actions"]

    decision = client.post(
        f"/approvals/{approval['id']}/decision",
        json={"decision": "approve", "reason": "looks fine"},
    )
    assert decision.status_code == 409
