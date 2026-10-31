from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg import Connection, sql
from psycopg.types.json import Jsonb
from starlette.requests import Request

from app.application_repositories import ApplicationRepositories
from app import application_repository_access
from app.auth import require_reviewer
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuthUser,
    Project,
    RiskAssessment,
    RiskLevel,
    User,
    UserRole,
    Workspace,
    now_utc,
)
from app.postgres_database import run_postgres_migrations
from app.trust_models import (
    ReviewerAttestationCreate,
    ReviewerAttestationDecision,
)
from app.trust_routes import create_attestation, list_attestations, router as trust_router
from app.trust_service import record_reviewer_attestation_postgres


@pytest.fixture
def postgres_attestation_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]]]:
    dsn = os.getenv("AIXION_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("AIXION_TEST_POSTGRES_DSN is required for PostgreSQL integration tests")

    schema = f"ct_attestation_route_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect() -> Connection:
        connection = psycopg.connect(dsn)
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return connection

    workspace_id = f"workspace_{uuid.uuid4().hex}"
    owner_id = f"user_{uuid.uuid4().hex}"
    reviewer_id = f"user_{uuid.uuid4().hex}"
    project_id = f"project_{uuid.uuid4().hex}"
    approval_id = f"approval_{uuid.uuid4().hex}"
    workspace = Workspace(id=workspace_id, name="Attestation test", owner_user_id=owner_id)
    owner = User(
        id=owner_id,
        email=f"{owner_id}@example.test",
        display_name="Approval owner",
        role=UserRole.OWNER,
        password_hash="test-password-hash",
        password_salt="test-password-salt",
        workspace_id=workspace_id,
        email_verified=True,
    )
    reviewer = User(
        id=reviewer_id,
        email=f"{reviewer_id}@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
        password_hash="test-password-hash",
        password_salt="test-password-salt",
        workspace_id=workspace_id,
        email_verified=True,
    )
    project = Project(
        id=project_id,
        name="Attestation project",
        workspace_id=workspace_id,
        created_by_user_id=owner_id,
    )
    approval = ApprovalRequest(
        id=approval_id,
        project_id=project_id,
        title="Reviewed action",
        summary="An approved immutable action payload",
        target_branch="feature/attestation-test",
        files=[],
        risk=RiskAssessment(level=RiskLevel.MEDIUM),
        status=ApprovalStatus.APPROVED,
        created_by_user_id=owner_id,
        approved_by_user_id=owner_id,
        workspace_id=workspace_id,
        approved_payload_hash_version=2,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    approval = approval.model_copy(
        update={"approved_payload_hash": compute_approval_payload_hash(approval)}
    )

    try:
        run_postgres_migrations(connect)
        with connect() as conn:
            conn.execute(
                "INSERT INTO workspaces(workspace_id,payload) VALUES(%s,%s)",
                (workspace_id, Jsonb(workspace.model_dump(mode="json"))),
            )
            for user in (owner, reviewer):
                conn.execute(
                    "INSERT INTO users(user_id,workspace_id,email,normalized_email,role,disabled,"
                    "email_verified,payload) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        user.id,
                        workspace_id,
                        user.email,
                        user.email.lower(),
                        user.role.value,
                        user.disabled,
                        user.email_verified,
                        Jsonb(user.model_dump(mode="json")),
                    ),
                )
            conn.execute(
                "INSERT INTO projects(project_id,workspace_id,payload) VALUES(%s,%s,%s)",
                (project_id, workspace_id, Jsonb(project.model_dump(mode="json"))),
            )
            conn.execute(
                "INSERT INTO approval_requests(approval_id,project_id,workspace_id,work_order_id,"
                "source_agent_id,status,expires_at,created_at,updated_at,version,source_invocation_id,payload) "
                "VALUES(%s,%s,%s,NULL,NULL,%s,NULL,%s,%s,0,NULL,%s)",
                (
                    approval_id,
                    project_id,
                    workspace_id,
                    approval.status.value,
                    approval.created_at,
                    approval.updated_at,
                    Jsonb(approval.model_dump(mode="json")),
                ),
            )

        monkeypatch.setenv("AIXION_PROFILE", "test")
        monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
        monkeypatch.setattr(
            application_repository_access,
            "get_settings",
            lambda: SimpleNamespace(auth_enabled=True, postgres_auth_enabled=True),
        )
        repositories = ApplicationRepositories.from_connector(connect)
        app = FastAPI()
        app.state.postgres_runtime = SimpleNamespace(
            connect=connect,
            repositories=repositories,
        )
        app.state.postgres_repositories = repositories
        app.state.approval_decision_repository = repositories.approval_decisions
        app.state.workspace_membership_repository = repositories.workspace_membership
        app.state.trust_repository = repositories.trust
        app.state.trust_event_repository = repositories.trust_events
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/trust/attestations",
                "headers": [],
                "query_string": b"",
                "app": app,
            }
        )
        auth_user = AuthUser(
            id=reviewer.id,
            email=reviewer.email,
            display_name=reviewer.display_name,
            role=reviewer.role,
            workspace_id=workspace_id,
            email_verified=True,
        )
        app.include_router(trust_router)
        app.dependency_overrides[require_reviewer] = lambda: auth_user
        yield request, auth_user, approval, connect
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_route_records_and_lists_attestation_with_one_ledger_event(
    postgres_attestation_runtime: tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]],
) -> None:
    request, reviewer, approval, connect = postgres_attestation_runtime
    payload = ReviewerAttestationCreate(
        approval_request_id=approval.id,
        decision=ReviewerAttestationDecision.APPROVE,
        reason="Reviewed exact action",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, competing = list(
            pool.map(
                lambda _: create_attestation(payload, reviewer, request), range(2)
            )
        )
    retry = create_attestation(payload, reviewer, request)
    listed = list_attestations(approval.id, reviewer, request)
    with connect() as conn:
        attestation_count = conn.execute(
            "SELECT COUNT(*) FROM reviewer_attestations WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM trust_events WHERE entity_id=%s AND event_type='ATTESTATION_RECORDED'",
            (approval.id,),
        ).fetchone()[0]

    assert first == retry
    assert competing == first
    assert listed == [first]
    assert attestation_count == 1
    assert event_count == 1
    verification = request.app.state.postgres_repositories.trust_events.verify()
    assert verification.valid
    assert verification.event_count == 1


def test_postgres_attestation_is_selected_through_fastapi_request_injection(
    postgres_attestation_runtime: tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]],
) -> None:
    request, reviewer, approval, _ = postgres_attestation_runtime
    client = TestClient(request.app)
    payload = {
        "approval_request_id": approval.id,
        "decision": "APPROVE",
        "reason": "HTTP routed approval",
    }

    created = client.post("/trust/attestations", json=payload)
    listed = client.get(
        "/trust/attestations", params={"approval_request_id": approval.id}
    )

    assert created.status_code == 200, created.text
    assert created.json()["reviewer_user_id"] == reviewer.id
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [created.json()["id"]]


def test_postgres_route_rejects_conflicting_replay(
    postgres_attestation_runtime: tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]],
) -> None:
    request, reviewer, approval, _ = postgres_attestation_runtime
    create_attestation(
        ReviewerAttestationCreate(
            approval_request_id=approval.id,
            decision=ReviewerAttestationDecision.APPROVE,
            reason="Original reason",
        ),
        reviewer,
        request,
    )

    with pytest.raises(HTTPException) as error:
        create_attestation(
            ReviewerAttestationCreate(
                approval_request_id=approval.id,
                decision=ReviewerAttestationDecision.DENY,
                reason="Conflicting replay",
            ),
            reviewer,
            request,
        )

    assert error.value.status_code == 409


def test_postgres_attestation_rolls_back_when_ledger_append_fails(
    postgres_attestation_runtime: tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]],
) -> None:
    request, reviewer, approval, connect = postgres_attestation_runtime

    class FailingEventRepository:
        @staticmethod
        def append_in_transaction(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated ledger write failure")

    with pytest.raises(RuntimeError, match="simulated ledger write failure"):
        record_reviewer_attestation_postgres(
            ReviewerAttestationCreate(
                approval_request_id=approval.id,
                decision=ReviewerAttestationDecision.APPROVE,
                reason="Must roll back",
            ),
            user=reviewer,
            workspace_id=reviewer.workspace_id or "",
            connect=connect,
            trust_repository=request.app.state.postgres_repositories.trust,
            trust_event_repository=FailingEventRepository(),  # type: ignore[arg-type]
        )

    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reviewer_attestations WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trust_events").fetchone()[0] == 0


def test_postgres_attestation_route_rejects_mismatched_workspace_claim(
    postgres_attestation_runtime: tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]],
) -> None:
    request, reviewer, approval, connect = postgres_attestation_runtime
    mismatched_user = reviewer.model_copy(update={"workspace_id": "workspace_other"})

    with pytest.raises(HTTPException) as error:
        create_attestation(
            ReviewerAttestationCreate(
                approval_request_id=approval.id,
                decision=ReviewerAttestationDecision.APPROVE,
            ),
            mismatched_user,
            request,
        )

    assert error.value.status_code == 403
    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reviewer_attestations WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trust_events").fetchone()[0] == 0


def test_postgres_attestation_treats_corrupt_approval_payload_as_integrity_failure(
    postgres_attestation_runtime: tuple[object, AuthUser, ApprovalRequest, Callable[[], Connection]],
) -> None:
    request, reviewer, approval, connect = postgres_attestation_runtime
    with connect() as conn:
        conn.execute(
            "UPDATE approval_requests SET payload=payload-'title' WHERE approval_id=%s",
            (approval.id,),
        )

    with pytest.raises(RuntimeError, match="Stored approval payload is invalid"):
        record_reviewer_attestation_postgres(
            ReviewerAttestationCreate(
                approval_request_id=approval.id,
                decision=ReviewerAttestationDecision.APPROVE,
            ),
            user=reviewer,
            workspace_id=reviewer.workspace_id or "",
            connect=connect,
            trust_repository=request.app.state.postgres_repositories.trust,
            trust_event_repository=request.app.state.postgres_repositories.trust_events,
        )

    with connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM reviewer_attestations WHERE approval_id=%s",
            (approval.id,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM trust_events").fetchone()[0] == 0
