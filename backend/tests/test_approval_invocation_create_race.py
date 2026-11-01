from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app
from app.models import Project, Workspace
from app.store import store


def test_concurrent_same_invocation_requests_reuse_one_durable_approval(
    monkeypatch,
) -> None:
    """Concurrent retries for one invocation must not fork approval records."""
    store.reset()
    workspace = Workspace(name="Invocation race", owner_user_id="dev_user")
    project = Project(
        name="Invocation race project",
        workspace_id=workspace.id,
    )
    store.workspaces[workspace.id] = workspace
    store.projects[project.id] = project
    store.persist()

    with TestClient(app) as client:
        registration = client.post(
            "/agents",
            json={
                "provider": "ANTIGRAVITY",
                "display_name": "Concurrent retry connector",
                "auth_type": "API_KEY",
                "allowed_project_ids": [project.id],
                "allowed_actions": [
                    "CREATE_APPROVAL",
                    "READ_APPROVAL_DECISION",
                    "CONSUME_APPROVAL_DECISION",
                    "EXPIRE_OWN_APPROVAL",
                ],
                "enabled": True,
            },
        )
        assert registration.status_code == 200, registration.text
        body = registration.json()
        headers = {
            "X-Aixion-Agent-Id": body["agent"]["id"],
            "X-Aixion-Agent-Token": body["agent_token"],
        }
        payload = {
            "project_id": project.id,
            "title": "Concurrent approval retry",
            "summary": "Both requests represent the same paused tool invocation.",
            "agent_name": "Antigravity",
            "target_branch": "current-workspace",
            "files": [
                {
                    "path": "antigravity://tool/run_command",
                    "change_type": "tool_call",
                    "diff": '{"command":"echo exactly-once"}',
                }
            ],
            "test_plan": ["Review the exact invocation"],
            "rollback_plan": "Deny the request or let its deadline expire.",
            "source_session_id": "session-concurrent-retry",
            "source_invocation_id": "antigravity:v1:concurrent-retry",
        }
        invocation_id = payload["source_invocation_id"]
        lookup_barrier = Barrier(2)
        repository = app.state.approval_decision_repository
        original_lookup = repository.get_by_invocation

        def synchronized_lookup(**kwargs):
            existing = original_lookup(**kwargs)
            if kwargs["source_invocation_id"] == invocation_id and existing is None:
                # Force both HTTP requests past the pre-insert idempotency
                # lookup before either can create the durable row.
                lookup_barrier.wait(timeout=5)
            return existing

        monkeypatch.setattr(repository, "get_by_invocation", synchronized_lookup)

        def submit():
            return client.post("/agents/approvals", headers=headers, json=payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = [
                future.result(timeout=15)
                for future in (executor.submit(submit), executor.submit(submit))
            ]

    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    approvals = [response.json() for response in responses]
    assert approvals[0]["id"] == approvals[1]["id"]
    approval_id = approvals[0]["id"]
    assert approvals[0]["source_invocation_id"] == payload["source_invocation_id"]
    assert sum(
        event.event_type == "approval.created_by_agent"
        and event.entity_id == approval_id
        for event in store.audit_events
    ) == 1
    assert sum(
        notification.entity_id == approval_id
        for notification in store.notifications.values()
    ) == 1
