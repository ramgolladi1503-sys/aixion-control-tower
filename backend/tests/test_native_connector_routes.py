from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_native_connector_inventory_is_exposed_fail_closed() -> None:
    response = client.get("/native-connectors")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 2

    native = next(
        item
        for item in payload
        if item["manifest"]["adapter_id"] == "codex-native-desktop"
    )
    managed = next(
        item
        for item in payload
        if item["manifest"]["adapter_id"]
        == "codex-aixion-managed-app-server"
    )

    assert native["manifest"]["mode"] == "NATIVE_EXISTING_SESSION"
    assert native["manifest"]["support"] == "PARTNER_REQUIRED"
    assert native["eligibility"]["native_control_eligible"] is False
    assert native["eligibility"]["customer_label"] == "Native session"

    assert managed["manifest"]["mode"] == "AIXION_MANAGED_SESSION"
    assert managed["manifest"]["support"] == "MANAGED_ONLY"
    assert managed["eligibility"]["native_control_eligible"] is False
    assert (
        managed["eligibility"]["customer_label"]
        == "Aixion-managed session"
    )


def test_native_connector_inventory_can_filter_provider_case_insensitively() -> None:
    response = client.get(
        "/native-connectors",
        params={"provider": "codex"},
    )

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_native_connector_detail_reports_provider_access_blocker() -> None:
    response = client.get(
        "/native-connectors/CODEX/codex-native-desktop"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["eligibility"]["native_control_eligible"] is False
    assert "support level is PARTNER_REQUIRED" in payload["eligibility"][
        "blockers"
    ]


def test_unknown_native_connector_returns_404() -> None:
    response = client.get(
        "/native-connectors/CODEX/not-registered"
    )

    assert response.status_code == 404
    assert "not registered" in response.json()["detail"]
