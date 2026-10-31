from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request, status

from .auth import require_user
from .models import AuthUser, UserRole
from .settings import get_settings

METRICS_SCRAPER_USER_ID = "prometheus_metrics_scraper"


def require_metrics_or_maintainer(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthUser:
    """Authenticate a workspace-bound scrape token or a maintainer session.

    The scrape token grants read-only metrics access to one explicitly
    configured workspace. It is deliberately not a cross-workspace service
    credential. Invalid tokens continue through normal session validation and
    cannot downgrade to an unauthenticated scrape.
    """
    settings = get_settings()
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization.split(" ", 1)[1].strip()
        if settings.metrics_token and hmac.compare_digest(
            supplied.encode("utf-8"), settings.metrics_token.encode("utf-8")
        ):
            if not settings.metrics_workspace_id:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Metrics workspace is not configured",
                )
            return AuthUser(
                id=METRICS_SCRAPER_USER_ID,
                email="prometheus-metrics@service.local",
                display_name="Prometheus metrics scraper",
                role=UserRole.MAINTAINER,
                workspace_id=settings.metrics_workspace_id,
                email_verified=True,
            )

    user = require_user(authorization=authorization, request=request)
    if user.role not in {UserRole.OWNER, UserRole.MAINTAINER}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Maintainer role required",
        )
    return user
