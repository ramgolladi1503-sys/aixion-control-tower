from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")
os.environ.setdefault("AIXION_PROFILE", "test")

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


def _register() -> tuple[str, str]:
    response = client.post(
        "/connectors/relay-hosts/register",
        json={
            "name": "Route relay",
            "platform": "MACOS",
            "hostname": "route.local",
            "machine_fingerprint": "r" * 64,
            "workspace_roots": ["/Users/test/work"],
            "allowed_repositories": ["owner/repo"],
            "adapters": [
                {
                    "adapter_id": "openclaw-gateway",
                    "provider": "OPENCLAW",
                    "adapter_kind": "OPENCLAW_GATEWAY",
                    "display_name": "OpenClaw",
                    "available": True,
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["relay"]["id"], payload["relay_token"]


def _headers(token: str) -> dict[str, str]:
    return {"X-Aixion-Relay-Token": token}


def test_relay_registration_heartbeat_session_and_command_cycle() -> None:
    relay_id, token = _register()

    unauthenticated = client.post(
        f"/connectors/relay-hosts/{relay_id}/heartbeat",
        json={"relay_version": "0.1.0"},
    )
    assert unauthenticated.status_code == 401

    heartbeat = client.post(
        f"/connectors/relay-hosts/{relay_id}/heartbeat",
        headers=_headers(token),
        json={"relay_version": "0.1.0", "active_session_count": 0},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["status"] == "ONLINE"

    created = client.post(
        "/connectors/relay-sessions",
        json={
            "relay_id": relay_id,
            "provider": "OPENCLAW",
            "adapter_id": "openclaw-gateway",
            "objective": "Review the approved repository safely.",
            "workspace_path": "/Users/test/work/repo",
            "repository": "owner/repo",
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]

    claim = client.post(
        f"/connectors/relay-hosts/{relay_id}/commands/claim",
        headers=_headers(token),
        json={"worker_id": "route-worker", "lease_seconds": 120},
    )
    assert claim.status_code == 200
    command = claim.json()["command"]
    assert command["command_type"] == "START_SESSION"
    assert command["lease_token"]

    snapshot = client.get(
        f"/connectors/relay-hosts/{relay_id}/sessions/{session_id}",
        headers=_headers(token),
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["session"]["id"] == session_id
    assert snapshot.json()["relay"]["id"] == relay_id

    ack = client.post(
        f"/connectors/relay-hosts/{relay_id}/commands/{command['id']}/ack",
        headers=_headers(token),
        json={
            "worker_id": "route-worker",
            "lease_token": command["lease_token"],
        },
    )
    assert ack.status_code == 200
    assert ack.json()["status"] == "ACKNOWLEDGED"

    completed = client.post(
        f"/connectors/relay-hosts/{relay_id}/commands/{command['id']}/complete",
        headers=_headers(token),
        json={
            "worker_id": "route-worker",
            "lease_token": command["lease_token"],
            "success": True,
            "remote_session_id": "openclaw-remote-1",
            "result": {"started": True},
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "SUCCEEDED"


def test_relay_cannot_read_or_mutate_another_relays_session() -> None:
    first_id, first_token = _register()
    store.relay_hosts[first_id].machine_fingerprint = "first-rotated-fingerprint"
    store.persist()
    second_id, second_token = _register()

    session = client.post(
        "/connectors/relay-sessions",
        json={
            "relay_id": first_id,
            "provider": "OPENCLAW",
            "adapter_id": "openclaw-gateway",
            "objective": "First relay task.",
            "workspace_path": "/Users/test/work/repo",
            "repository": "owner/repo",
        },
    ).json()["session"]

    forbidden = client.get(
        f"/connectors/relay-hosts/{second_id}/sessions/{session['id']}",
        headers=_headers(second_token),
    )
    assert forbidden.status_code == 403

    allowed = client.get(
        f"/connectors/relay-hosts/{first_id}/sessions/{session['id']}",
        headers=_headers(first_token),
    )
    assert allowed.status_code == 200
