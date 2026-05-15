from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "true")
os.environ.setdefault("AIXION_PROFILE", "test")

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)
PASSWORD = "valid-password-123"


def setup_function() -> None:
    store.reset()


def _register(email: str = "owner@example.com") -> dict:
    response = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": "Owner",
        },
    )
    assert response.status_code == 200
    return response.json()


def test_registration_requires_email_verification_and_does_not_create_session() -> None:
    payload = _register()

    assert payload["verification_required"] is True
    assert payload["user"]["email_verified"] is False
    assert payload["dev_verification_code"]
    assert "access_token" not in payload
    assert store.sessions == {}


def test_unverified_user_cannot_login_until_email_is_verified() -> None:
    payload = _register()

    blocked = client.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert blocked.status_code == 403
    assert "Email verification required" in blocked.text

    verified = client.post(
        "/auth/verify-email",
        json={"email": "owner@example.com", "code": payload["dev_verification_code"]},
    )
    assert verified.status_code == 200
    assert verified.json()["user"]["email_verified"] is True

    login = client.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["access_token"]
    assert login.json()["user"]["email_verified"] is True


def test_invalid_verification_code_is_rejected() -> None:
    _register()

    response = client.post(
        "/auth/verify-email",
        json={"email": "owner@example.com", "code": "000000"},
    )

    assert response.status_code == 400
    assert "Invalid verification code" in response.text


def test_resend_verification_rotates_dev_code() -> None:
    first = _register()

    resent = client.post("/auth/resend-verification", json={"email": "owner@example.com"})

    assert resent.status_code == 200
    assert resent.json()["verification_required"] is True
    assert resent.json()["dev_verification_code"]
    assert resent.json()["dev_verification_code"] != first["dev_verification_code"]


def test_verified_user_can_read_me_with_bearer_session() -> None:
    payload = _register()
    client.post(
        "/auth/verify-email",
        json={"email": "owner@example.com", "code": payload["dev_verification_code"]},
    )
    token = client.post("/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).json()["access_token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "owner@example.com"
    assert response.json()["email_verified"] is True
