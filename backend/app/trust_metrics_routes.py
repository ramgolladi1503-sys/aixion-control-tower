from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .auth import require_maintainer
from .models import AuthUser
from .trust_metrics import prometheus_trust_metrics
from .workspace_access import workspace_id_for_user

router = APIRouter(prefix="/trust", tags=["agent-trust"])
MaintainerDependency = Depends(require_maintainer)


@router.get("/metrics")
def get_trust_metrics(user: AuthUser = MaintainerDependency) -> Response:
    return Response(
        content=prometheus_trust_metrics(workspace_id_for_user(user)),
        media_type="text/plain; version=0.0.4",
    )
