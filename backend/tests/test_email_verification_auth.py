from __future__ import annotations
import json
import pytest
from types import SimpleNamespace



from fastapi.testclient import TestClient

from app.main import app
from app.auth import create_registration_response, create_user
from app.store import store

client = TestClient(app)
PASSWORD = "ValidPassword123!"




@pytest.fixture(autouse=True)
def reset_auth_store(monkeypatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_PROFILE", "test")
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


def test_local_registration_queues_encrypted_verification_code(monkeypatch) -> None:
    # SQLite outbox coverage belongs to local mode. Production auth now fails
    # closed unless the PostgreSQL repository is supplied explicitly.
    monkeypatch.setenv("AIXION_PROFILE", "local")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@test.invalid")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", bytes(range(32)).hex())
    user = create_user("owner@example.com", PASSWORD, email_verified=False)

    response = create_registration_response(user)

    assert response.dev_verification_code
    with store._connect() as conn:
        row = conn.execute(
            "SELECT recipient, encrypted_code, status FROM auth_email_outbox ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row[0] == "owner@example.com"
    assert b"123456" not in row[1]
    assert row[2] == "PENDING"


def test_outbox_insert_failure_rolls_back_verification_hash_and_memory(monkeypatch) -> None:
    # Exercise the SQLite transaction rollback path in its supported local mode.
    monkeypatch.setenv("AIXION_PROFILE", "local")
    monkeypatch.setenv("AIXION_EMAIL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("AIXION_EMAIL_FROM", "verify@test.invalid")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("AIXION_EMAIL_SMTP_PASSWORD", "smtp-password")
    monkeypatch.setenv("AIXION_EMAIL_OUTBOX_KEY", bytes(range(32)).hex())
    user = create_user("owner@example.com", PASSWORD, email_verified=False)

    def reject_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated outbox persistence failure")

    monkeypatch.setattr("app.auth.insert_verification_email", reject_insert)
    with pytest.raises(RuntimeError, match="simulated outbox persistence failure"):
        create_registration_response(user)

    assert store.users[user.id].email_verification_hash is None
    with store._connect() as conn:
        user_payload = conn.execute(
            "SELECT payload FROM kv_store WHERE entity_type='user' AND entity_id=?",
            (user.id,),
        ).fetchone()[0]
        queued = conn.execute("SELECT COUNT(*) FROM auth_email_outbox").fetchone()[0]
    assert json.loads(user_payload)["email_verification_hash"] is None
    assert queued == 0


@pytest.mark.parametrize(
    ("is_production", "postgres_auth_enabled"),
    [(True, False), (False, True)],
    ids=["production", "postgres-auth"],
)
def test_registration_response_requires_postgres_repository_in_authoritative_modes(
    monkeypatch, is_production: bool, postgres_auth_enabled: bool
) -> None:
    user = create_user("owner@example.com", PASSWORD, email_verified=False)
    user_before = user.model_copy(deep=True)
    with store._connect() as conn:
        outbox_before = conn.execute("SELECT COUNT(*) FROM auth_email_outbox").fetchone()[0]

    monkeypatch.setattr(
        "app.auth.get_settings",
        lambda: SimpleNamespace(
            is_production=is_production,
            postgres_auth_enabled=postgres_auth_enabled,
        ),
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        create_registration_response(user)

    assert error.value.status_code == 503
    assert error.value.headers == {"Retry-After": "30"}
    assert store.users[user.id] == user_before
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM auth_email_outbox").fetchone()[0] == outbox_before
