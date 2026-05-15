from __future__ import annotations

import os

os.environ.setdefault("AIXION_AUTH_ENABLED", "true")
os.environ.setdefault("AIXION_PROFILE", "test")

import pytest
from fastapi.testclient import TestClient

from app.email_policy import validate_registration_email_deliverability
from app.main import app
from app.store import store

client = TestClient(app)
PASSWORD = "StrongPass123!"


def setup_function() -> None:
    store.reset()


def resolver_accepts(domain: str) -> bool:
    return True


def resolver_rejects(domain: str) -> bool:
    return False


@pytest.mark.parametrize(
    "email",
    [
        "owner@gmail.com",
        "qa.user@outlook.com",
        "mobile.approver@company.co.in",
    ],
)
def test_email_policy_accepts_public_resolvable_domains(email: str) -> None:
    assert validate_registration_email_deliverability(email, resolver_accepts) == email.lower()


@pytest.mark.parametrize(
    "email,expected",
    [
        ("owner@localhost", "publicly deliverable"),
        ("owner@app.local", "publicly deliverable"),
        ("owner@example.test", "publicly deliverable"),
        ("owner@invalid", "publicly deliverable"),
        ("owner@internal", "public suffix"),
        ("owner@missing-mail-domain.example", "receive mail"),
    ],
)
def test_email_policy_rejects_non_deliverable_or_reserved_domains(email: str, expected: str) -> None:
    resolver = resolver_rejects if "missing-mail-domain" in email else resolver_accepts

    with pytest.raises(ValueError) as exc_info:
        validate_registration_email_deliverability(email, resolver)

    assert expected.lower() in str(exc_info.value).lower()


def test_registration_rejects_local_email_domain_before_creating_user() -> None:
    response = client.post(
        "/auth/register",
        json={
            "email": "owner@localhost",
            "password": PASSWORD,
            "display_name": "Owner",
        },
    )

    assert response.status_code == 422
    assert not store.users


def test_registration_rejects_single_label_email_domain_before_creating_user() -> None:
    response = client.post(
        "/auth/register",
        json={
            "email": "owner@internal",
            "password": PASSWORD,
            "display_name": "Owner",
        },
    )

    assert response.status_code == 422
    assert not store.users
