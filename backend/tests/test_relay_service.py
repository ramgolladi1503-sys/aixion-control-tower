from __future__ import annotations

import os
from datetime import timedelta

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")

import pytest

from app.models import AuthUser, UserRole, now_utc
from app.relay_auth import verify_relay_token
from app.relay_models import (
    RelayAdapterKind,
    RelayAdapterManifest,
    RelayCommandClaimRequest,
    RelayCommandStatus,
    RelayEventCreate,
    RelayEventType,
    RelayPlatform,
    RelayProvider,
    RelayRegistrationCreate,
    RelaySessionCreate,
    RelaySessionStatus,
)
from app.relay_service import (
    RelayConflict,
    acknowledge_command,
    append_relay_event,
    claim_relay_command,
    create_relay_session,
    register_relay,
    relay_summary,
    verify_session_event_chain,
)
from app.store import store


def setup_function() -> None:
    store.reset()


def _owner() -> AuthUser:
    return AuthUser(
        id="owner",
        email="owner@example.com",
        display_name="Owner",
        role=UserRole.OWNER,
        email_verified=True,
    )


def _registration() -> RelayRegistrationCreate:
    return RelayRegistrationCreate(
        name="Mac relay",
        platform=RelayPlatform.MACOS,
        hostname="mac.local",
        machine_fingerprint="f" * 64,
        workspace_roots=["/Users/test/work"],
        allowed_repositories=["owner/repo"],
        adapters=[
            RelayAdapterManifest(
                adapter_id="codex-app-server",
                provider=RelayProvider.CODEX,
                adapter_kind=RelayAdapterKind.CODEX_APP_SERVER,
                display_name="Codex",
            ),
            RelayAdapterManifest(
                adapter_id="antigravity-hooks",
                provider=RelayProvider.ANTIGRAVITY,
                adapter_kind=RelayAdapterKind.ANTIGRAVITY_HOOKS,
                display_name="Antigravity",
            ),
        ],
    )


def _relay_and_token():
    response = register_relay(_registration(), user=_owner())
    return store.relay_hosts[response.relay.id], response.relay_token


def _session(relay_id: str, provider=RelayProvider.CODEX, adapter="codex-app-server"):
    return create_relay_session(
        RelaySessionCreate(
            relay_id=relay_id,
            provider=provider,
            adapter_id=adapter,
            objective="Implement the approved change.",
            workspace_path="/Users/test/work/repo",
            repository="owner/repo",
        ),
        user=_owner(),
    )


def test_registration_returns_token_once_and_stores_only_hash() -> None:
    relay, token = _relay_and_token()
    assert token.startswith("aixr_")
    assert relay.token_hash != token
    assert verify_relay_token(token, relay.token_hash)

    with pytest.raises(RelayConflict, match="already exists"):
        register_relay(_registration(), user=_owner())


def test_session_enforces_provider_adapter_workspace_and_repository_scope() -> None:
    relay, _ = _relay_and_token()
    session = _session(
        relay.id,
        provider=RelayProvider.ANTIGRAVITY,
        adapter="antigravity-hooks",
    )
    assert session.provider == RelayProvider.ANTIGRAVITY
    assert len(store.relay_commands) == 1

    with pytest.raises(RelayConflict, match="workspace roots"):
        create_relay_session(
            RelaySessionCreate(
                relay_id=relay.id,
                provider=RelayProvider.CODEX,
                adapter_id="codex-app-server",
                objective="Escape workspace.",
                workspace_path="/tmp/outside",
                repository="owner/repo",
            ),
            user=_owner(),
        )

    with pytest.raises(RelayConflict, match="repository allowlist"):
        create_relay_session(
            RelaySessionCreate(
                relay_id=relay.id,
                provider=RelayProvider.CODEX,
                adapter_id="codex-app-server",
                objective="Wrong repository.",
                workspace_path="/Users/test/work/repo",
                repository="other/repo",
            ),
            user=_owner(),
        )


def test_acknowledged_command_expiry_is_recovered_without_duplicate_command() -> None:
    relay, _ = _relay_and_token()
    session = _session(relay.id)
    command = claim_relay_command(
        relay,
        RelayCommandClaimRequest(worker_id="worker-1", lease_seconds=30),
    )
    assert command is not None
    lease_token = command.lease_token
    assert lease_token
    acknowledge_command(
        command,
        relay=relay,
        worker_id="worker-1",
        lease_token=lease_token,
    )
    command.lease_expires_at = now_utc() - timedelta(seconds=1)

    recovered = claim_relay_command(
        relay,
        RelayCommandClaimRequest(worker_id="worker-2", lease_seconds=30),
    )
    assert recovered is command
    assert recovered.attempt_count == 2
    assert recovered.lease_owner == "worker-2"
    assert len([item for item in store.relay_commands.values() if item.session_id == session.id]) == 1


def test_event_chain_is_ordered_idempotent_and_tamper_evident() -> None:
    relay, _ = _relay_and_token()
    session = _session(relay.id)
    first_payload = RelayEventCreate(
        event_id="event-1",
        sequence=1,
        event_type=RelayEventType.SESSION_STARTED,
        message="Started",
        remote_session_id="remote-1",
    )
    first = append_relay_event(relay, session, first_payload)
    duplicate = append_relay_event(relay, session, first_payload)
    assert duplicate.id == first.id

    second = append_relay_event(
        relay,
        session,
        RelayEventCreate(
            event_id="event-2",
            sequence=2,
            event_type=RelayEventType.SESSION_COMPLETED,
            message="Completed",
        ),
    )
    assert second.previous_hash == first.event_hash
    assert session.status == RelaySessionStatus.COMPLETED
    assert session.final_evidence_hash == second.event_hash
    assert verify_session_event_chain(session) == (True, "")

    second.message = "tampered"
    valid, reason = verify_session_event_chain(session)
    assert valid is False
    assert "Hash mismatch" in reason


def test_relay_summary_exposes_queue_and_session_truth() -> None:
    relay, _ = _relay_and_token()
    _session(relay.id)
    summary = relay_summary()
    assert summary.total_relays == 1
    assert summary.total_sessions == 1
    assert summary.active_sessions == 1
    assert summary.pending_commands == 1
