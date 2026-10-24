from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from .auth import require_maintainer
from .postgres_trust_event_repository import PostgresTrustEventRepositoryError
from .postgres_trust_repository import PostgresTrustRepositoryError
from .models import AuthUser, now_utc
from .trust_metrics import prometheus_trust_metrics
from .workspace_access import workspace_id_for_user

router = APIRouter(prefix="/trust", tags=["agent-trust"])
MaintainerDependency = Depends(require_maintainer)


@router.get("/metrics")
def get_trust_metrics(
    request: Request,
    user: AuthUser = MaintainerDependency,
) -> Response:
    runtime = getattr(request.app.state, "postgres_runtime", None)
    repositories = getattr(request.app.state, "postgres_repositories", None)
    selected_bindings = False
    if runtime is not None and repositories is not None:
        required_bindings = {
            "approval_decision_repository": "approval_decisions",
            "workspace_membership_repository": "workspace_membership",
            "trust_repository": "trust",
            "trust_event_repository": "trust_events",
            "project_repository": "projects",
            "notification_outbox_repository": "notification_outbox",
        }
        selected_bindings = all(
            getattr(request.app.state, state_name, None)
            is getattr(repositories, repository_name, None)
            for state_name, repository_name in required_bindings.items()
        )
        partial_bindings = any(
            getattr(request.app.state, state_name, None)
            is getattr(repositories, repository_name, None)
            for state_name, repository_name in required_bindings.items()
        )
        foreign_postgres_binding = any(
            getattr(request.app.state, state_name, None) is not None
            and type(getattr(request.app.state, state_name)).__name__.startswith("Postgres")
            and getattr(request.app.state, state_name)
            is not getattr(repositories, repository_name, None)
            for state_name, repository_name in required_bindings.items()
        )
        if selected_bindings and repositories is not runtime.repositories:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL trust metrics require the app-bound repository runtime",
            )
        if (partial_bindings and not selected_bindings) or foreign_postgres_binding:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL trust metrics require all trust and notification authorities to share one runtime",
            )
    elif runtime is not None or repositories is not None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL trust metrics require the complete app-bound repository bundle",
        )

    if selected_bindings:
        workspace_id = workspace_id_for_user(
            user,
            membership_repository=repositories.workspace_membership,
        )
    else:
        workspace_id = workspace_id_for_user(user)

    if selected_bindings:
        try:
            with runtime.connect() as conn:
                with conn.transaction():
                    conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    snapshot = repositories.trust.metrics_snapshot(
                        conn,
                        workspace_id=workspace_id,
                        now=now_utc(),
                        trust_event_repository=repositories.trust_events,
                    )
        except (
            psycopg.Error,
            PostgresTrustRepositoryError,
            PostgresTrustEventRepositoryError,
            ValueError,
        ) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PostgreSQL trust metrics are unavailable",
            ) from error
        content = prometheus_trust_metrics(
            workspace_id,
            postgres_snapshot=snapshot,
        )
    else:
        content = prometheus_trust_metrics(workspace_id)
    return Response(
        content=content,
        media_type="text/plain; version=0.0.4",
    )
