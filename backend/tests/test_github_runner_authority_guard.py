from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import application_repository_access, github_runner
from app.auth import require_maintainer
from app.main import app
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    FileChange,
    RiskAssessment,
    RiskLevel,
    UserRole,
)
from app.store import store


class ReadTrap(dict):
    def get(self, *_args, **_kwargs):
        pytest.fail("Authoritative mode must not read SQLite approval state")


def _user() -> AuthUser:
    return AuthUser(
        id="github-runner-test-owner",
        email="owner@example.test",
        display_name="Owner",
        role=UserRole.OWNER,
        workspace_id="workspace-test",
        email_verified=True,
    )


def _approved_request() -> ApprovalRequest:
    approval = ApprovalRequest(
        project_id="project-test",
        workspace_id="workspace-test",
        title="Approved docs change",
        summary="Update approved docs content",
        agent_name="test-agent",
        target_branch="feature/docs-change",
        files=[
            FileChange(
                path="docs/README.md",
                change_type="update",
                diff="+approved",
                new_content="approved content\n",
            )
        ],
        test_plan=[],
        rollback_plan="Revert docs commit",
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.APPROVED,
    )
    from app.approval_integrity import compute_approval_payload_hash

    approval.approved_payload_hash = compute_approval_payload_hash(approval)
    return approval


def _payload() -> dict[str, str]:
    return {
        "approval_request_id": "sqlite-only-approval",
        "repository_full_name": "owner/repository",
        "base_branch": "main",
        "feature_branch": "feature/docs-change",
    }


def _override_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(app.dependency_overrides, require_maintainer, _user)


def test_http_execute_fails_before_sqlite_or_github_side_effects_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.reset()
    monkeypatch.setattr(store, "approval_requests", ReadTrap())
    monkeypatch.setattr(store, "persist", lambda **_kwargs: pytest.fail("SQLite persist reached"))
    monkeypatch.setattr(
        github_runner,
        "_validate_execution",
        lambda *_args, **_kwargs: pytest.fail("Legacy validation reached"),
    )
    monkeypatch.setattr(
        github_runner,
        "_github",
        lambda *_args, **_kwargs: pytest.fail("GitHub API reached"),
    )
    monkeypatch.setattr(
        github_runner,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )
    _override_owner(monkeypatch)

    response = TestClient(app).post("/github-runner/execute", json=_payload())

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


def test_direct_execute_call_fails_before_local_state_in_postgres_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.reset()
    monkeypatch.setattr(store, "approval_requests", ReadTrap())
    monkeypatch.setattr(store, "persist", lambda **_kwargs: pytest.fail("SQLite persist reached"))
    monkeypatch.setattr(
        github_runner,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )
    payload = github_runner.ExecuteGitHubRequest(**_payload())

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            github_runner.execute_github_change(payload, request=None, _=_user())
        )

    assert raised.value.status_code == 503


def test_http_patch_plan_never_uses_sqlite_when_postgres_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.reset()
    monkeypatch.setattr(store, "approval_requests", ReadTrap())
    monkeypatch.setattr(
        github_runner,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        application_repository_access,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )
    _override_owner(monkeypatch)

    response = TestClient(app).post("/github-runner/patch-plan", json=_payload())

    assert response.status_code == 503
    assert "PostgreSQL patch-plan approval data is unavailable" in response.text


def test_production_patch_plan_without_postgres_auth_fails_closed_before_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.reset()
    monkeypatch.setattr(store, "approval_requests", ReadTrap())
    monkeypatch.setattr(
        github_runner,
        "get_settings",
        lambda: SimpleNamespace(is_production=True, postgres_auth_enabled=False),
    )
    _override_owner(monkeypatch)

    response = TestClient(app).post("/github-runner/patch-plan", json=_payload())

    assert response.status_code == 503
    assert "PostgreSQL patch planning requires PostgreSQL-backed authentication" in response.text


def test_probe_only_local_patch_plan_remains_available(monkeypatch: pytest.MonkeyPatch) -> None:
    store.reset()
    approval = _approved_request()
    store.approval_requests[approval.id] = approval
    monkeypatch.setattr(
        github_runner,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=False,
            postgres_auth_enabled=False,
            postgres_runtime_probe_enabled=True,
        ),
    )
    _override_owner(monkeypatch)
    payload = _payload()
    payload["approval_request_id"] = approval.id

    response = TestClient(app).post("/github-runner/patch-plan", json=payload)

    assert response.status_code == 200
    assert response.json()["ready"] is True
