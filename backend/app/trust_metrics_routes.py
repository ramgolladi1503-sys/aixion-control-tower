from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .auth import require_maintainer
from .models import AuthUser
from .trust_metrics import prometheus_trust_metrics

router = APIRouter(prefix="/trust", tags=["agent-trust"])
MaintainerDependency = Depends(require_maintainer)


@router.get("/metrics")
def get_trust_metrics(_: AuthUser = MaintainerDependency) -> Response:
    return Response(
        content=prometheus_trust_metrics(),
        media_type="text/plain; version=0.0.4",
    )
