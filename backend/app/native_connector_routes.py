from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .auth import require_owner
from .models import AuthUser
from .native_connector_contract import (
    NativeConnectorEligibility,
    NativeConnectorManifest,
)
from .native_connector_registry import (
    NativeConnectorNotFound,
    build_default_native_connector_registry,
)

router = APIRouter(
    prefix="/connectors/native-capabilities",
    tags=["native-connectors"],
)
OwnerDependency = Depends(require_owner)
_registry = build_default_native_connector_registry()


class NativeConnectorInventoryItem(BaseModel):
    manifest: NativeConnectorManifest
    eligibility: NativeConnectorEligibility


@router.get("", response_model=list[NativeConnectorInventoryItem])
def list_native_connector_capabilities(
    provider: str | None = None,
    _: AuthUser = OwnerDependency,
) -> list[NativeConnectorInventoryItem]:
    manifests = _registry.list(provider=provider)
    return [
        NativeConnectorInventoryItem(
            manifest=manifest,
            eligibility=_registry.eligibility(
                manifest.provider,
                manifest.adapter_id,
            ),
        )
        for manifest in manifests
    ]


@router.get(
    "/{provider}/{adapter_id}",
    response_model=NativeConnectorInventoryItem,
)
def get_native_connector_capability(
    provider: str,
    adapter_id: str,
    _: AuthUser = OwnerDependency,
) -> NativeConnectorInventoryItem:
    try:
        manifest = _registry.get(provider, adapter_id)
    except NativeConnectorNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return NativeConnectorInventoryItem(
        manifest=manifest,
        eligibility=_registry.eligibility(provider, adapter_id),
    )
