from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .auth import require_maintainer
from .models import AuthUser
from .relay_metrics import prometheus_relay_metrics

router = APIRouter(prefix="/relay", tags=["agent-relay"])
MaintainerDependency = Depends(require_maintainer)


@router.get("/metrics")
def get_relay_metrics(_: AuthUser = MaintainerDependency) -> Response:
    return Response(
        content=prometheus_relay_metrics(),
        media_type="text/plain; version=0.0.4",
    )
