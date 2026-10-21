from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app import main
from app.models import Project
from app.store import store

client = TestClient(main.app, raise_server_exceptions=False)


def setup_function() -> None:
    store.reset()


def _project() -> Project:
    project = Project(name="WorkOrder repository API", description="test")
    store.projects[project.id] = project
    store.persist()
    return project


def _payload(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "goal": "Persist the work order transactionally",
        "context": "The route and repository must agree on durable state.",
        "tasks": ["Create the work order", "Record the audit event"],
        "affected_areas": ["backend"],
        "required_tests": ["pytest tests/test_work_order_api_repository.py"],
        "rollback_plan": "Revert the repository-bound route change.",
    }


def test_work_order_list_reads_durable_repository_after_cache_loss() -> None:
    project = _project()
    created = client.post("/work-orders", json=_payload(project.id))
    assert created.status_code == 200
    work_order_id = created.json()["id"]

    store.work_orders.clear()
    response = client.get("/work-orders")

    assert response.status_code == 200
    assert any(item["id"] == work_order_id for item in response.json())
    assert work_order_id not in store.work_orders


def test_repository_failure_does_not_publish_work_order_or_audit(monkeypatch) -> None:
    project = _project()
    before_work_orders = set(store.work_orders)
    before_audits = {event.id for event in store.audit_events}

    class FailingWorkOrderRepository:
        def create(self, *, work_order, audit_event) -> None:
            raise RuntimeError("simulated repository failure")

    monkeypatch.setattr(
        main.app.state, "work_order_repository", FailingWorkOrderRepository()
    )
    response = client.post("/work-orders", json=_payload(project.id))

    assert response.status_code == 500
    assert set(store.work_orders) == before_work_orders
    assert {event.id for event in store.audit_events} == before_audits
