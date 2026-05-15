from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "true")
os.environ.setdefault("AIXION_PROFILE", "test")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.password_policy import validate_password_policy
from app.store import store

client = TestClient(app)


def setup_function() -> None:
    store.reset()


@pytest.mark.parametrize(
    "password",
    [
        "StrongPass123!",
        "BetterVault92#",
        "AixSafe-Runner-77",
        "MobileGate99$",
    ],
)
def test_password_policy_accepts_strong_passwords(password: str) -> None:
    assert validate_password_policy(password, "owner@example.com") == password


@pytest.mark.parametrize(
    "password,expected",
    [
        ("Short1!", "at least"),
        ("a" * 129 + "A1!", "at most"),
        ("lowercaseonly", "at least three"),
        ("UPPERCASEONLY", "at least three"),
        ("123456789012", "at least three"),
        ("NoNumbersHere!", "at least three"),
        ("NoSymbolsHere1", "at least three"),
        (" LeadingPass123!", "whitespace"),
        ("TrailingPass123! ", "whitespace"),
        ("Has Space123!", "whitespace"),
        ("Aaaaaaaa111!", "repeated"),
        ("OwnerSecure123!", "email username"),
        ("GoodPass123456!", "sequence"),
        ("AixionSecure99!", "product"),
        ("ControlTower99!", "product"),
    ],
)
def test_password_policy_rejects_weak_edge_cases(password: str, expected: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_password_policy(password, "owner@example.com")

    assert expected.lower() in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "password",
    [
        "Short1!",
        "lowercaseonly",
        "Has Space123!",
        "OwnerSecure123!",
        "GoodPass123456!",
        "AixionSecure99!",
    ],
)
def test_registration_endpoint_rejects_weak_passwords(password: str) -> None:
    response = client.post(
        "/auth/register",
        json={
            "email": "owner@example.com",
            "password": password,
            "display_name": "Owner",
        },
    )

    assert response.status_code == 422
    assert not store.users


def test_registration_endpoint_accepts_strong_password_and_still_requires_verification() -> None:
    response = client.post(
        "/auth/register",
        json={
            "email": "owner@example.com",
            "password": "StrongPass123!",
            "display_name": "Owner",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["verification_required"] is True
    assert payload["user"]["email_verified"] is False
    assert payload["dev_verification_code"]
