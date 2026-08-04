from __future__ import annotations

from .models import now_utc
from .relay_models import (
    RelayHost,
    RelayProvider,
    RelaySession,
    RelaySessionCreate,
    RelaySessionStatus,
    RelayStatus,
)
from .relay_service import RelayConflict, TERMINAL_SESSION_STATUSES
from .store import store
from .trust_crypto import sha256_hex

NATIVE_ANTIGRAVITY_ADAPTER_ID = "antigravity-native-hook"


def _normalize_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[0].lower() + normalized[1:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _path_within_root(path: str, root: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_root = _normalize_path(root)
    return normalized_path == normalized_root or normalized_path.startswith(
        normalized_root.rstrip("/") + "/"
    )


def _native_conversation_id(payload: RelaySessionCreate) -> str:
    value = str(payload.metadata.get("native_conversation_id") or "").strip()
    if not value:
        raise RelayConflict("native_conversation_id is required for native attachment.")
    if len(value) > 500:
        raise RelayConflict("native_conversation_id exceeds 500 characters.")
    return value


def _assert_native_antigravity_adapter(
    relay: RelayHost,
    payload: RelaySessionCreate,
) -> None:
    if payload.provider != RelayProvider.ANTIGRAVITY:
        raise RelayConflict("This endpoint currently supports native Antigravity only.")
    if payload.adapter_id != NATIVE_ANTIGRAVITY_ADAPTER_ID:
        raise RelayConflict(
            f"Native Antigravity must use adapter {NATIVE_ANTIGRAVITY_ADAPTER_ID!r}."
        )
    manifest = next(
        (
            item
            for item in relay.adapters
            if item.adapter_id == payload.adapter_id
            and item.provider == payload.provider
        ),
        None,
    )
    if manifest is None or not manifest.available:
        raise RelayConflict("Relay does not advertise the native Antigravity hook.")
    if manifest.metadata.get("native_attach_only") is not True:
        raise RelayConflict("Antigravity adapter is not marked native-attach-only.")


def attach_native_relay_session(
    relay: RelayHost,
    payload: RelaySessionCreate,
) -> RelaySession:
    """Attach an already-running provider conversation without launching it."""

    if payload.relay_id != relay.id:
        raise RelayConflict("Native session payload belongs to another relay.")
    if relay.status == RelayStatus.DISABLED:
        raise RelayConflict("Disabled relay cannot attach native sessions.")
    _assert_native_antigravity_adapter(relay, payload)

    if not any(
        _path_within_root(payload.workspace_path, root)
        for root in relay.workspace_roots
    ):
        raise RelayConflict("Workspace path is outside the relay workspace roots.")
    if relay.allowed_repositories:
        if not payload.repository or payload.repository not in relay.allowed_repositories:
            raise RelayConflict("Repository is outside the relay allowlist.")
    if relay.allowed_project_ids and payload.project_id not in relay.allowed_project_ids:
        raise RelayConflict("Project is outside the relay allowlist.")

    conversation_id = _native_conversation_id(payload)
    request_hash = sha256_hex(
        {
            "relay_id": relay.id,
            "provider": payload.provider,
            "adapter_id": payload.adapter_id,
            "workspace_path": _normalize_path(payload.workspace_path),
            "repository": payload.repository,
            "native_conversation_id": conversation_id,
        }
    )
    existing = next(
        (
            session
            for session in store.relay_sessions.values()
            if session.metadata.get("native_attachment_hash") == request_hash
            and session.status not in TERMINAL_SESSION_STATUSES
        ),
        None,
    )
    if existing:
        existing.status = RelaySessionStatus.RUNNING
        existing.remote_session_id = conversation_id
        existing.updated_at = now_utc()
        store.persist()
        return existing

    now = now_utc()
    session = RelaySession(
        **payload.model_dump(exclude={"metadata"}),
        status=RelaySessionStatus.RUNNING,
        remote_session_id=conversation_id,
        started_at=now,
        metadata={
            **payload.metadata,
            "native_attachment_hash": request_hash,
            "native_attach_only": True,
            "launches_replacement_agent": False,
            "created_by_relay_id": relay.id,
        },
    )
    store.relay_sessions[session.id] = session
    store.persist()
    return session
