from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.auth_rate_limits import consume_auth_rate_limit
from app.auth_routes import _limit
from app.main import app
from app.store import store

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_auth_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv("AIXION_AUTH_ENABLED", "true")
    monkeypatch.setenv("AIXION_AUTH_RATE_LIMIT_HMAC_KEY", "ab" * 32)
    store.reset()


def test_fixed_window_blocks_after_limit_and_resets_at_next_window() -> None:
    assert consume_auth_rate_limit(
        bucket="login", identifier="192.0.2.10", limit=2, window_seconds=60, now_epoch=125
    ) is None
    assert consume_auth_rate_limit(
        bucket="login", identifier="192.0.2.10", limit=2, window_seconds=60, now_epoch=125
    ) is None
    assert consume_auth_rate_limit(
        bucket="login", identifier="192.0.2.10", limit=2, window_seconds=60, now_epoch=125
    ) == 55
    assert consume_auth_rate_limit(
        bucket="login", identifier="192.0.2.10", limit=2, window_seconds=60, now_epoch=180
    ) is None


def test_parallel_requests_cannot_exceed_fixed_window_limit() -> None:
    def consume(_: int) -> int | None:
        return consume_auth_rate_limit(
            bucket="register", identifier="192.0.2.20", limit=3, window_seconds=60, now_epoch=620
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(consume, range(8)))

    assert sum(result is None for result in results) == 3
    assert all(result is None or result == 40 for result in results)


def test_rate_limit_storage_contains_only_hmac_digest() -> None:
    raw_identifier = "owner@example.com"
    consume_auth_rate_limit(
        bucket="resend-email", identifier=raw_identifier, limit=1, window_seconds=60, now_epoch=120
    )

    with store._connect() as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(auth_rate_limit_buckets)")]
        rows = conn.execute("SELECT bucket_hash FROM auth_rate_limit_buckets").fetchall()

    assert columns == ["bucket_hash", "window_start", "request_count"]
    assert len(rows) == 1
    assert len(rows[0][0]) == 64
    assert raw_identifier not in rows[0][0]


def test_postgres_repository_is_selected_without_sqlite_write() -> None:
    class FakeRepository:
        calls: list[dict[str, object]] = []

        def consume(self, **kwargs: object) -> int | None:
            self.calls.append(kwargs)
            return None

    repository = FakeRepository()
    with store._connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM auth_rate_limit_buckets").fetchone()[0]

    result = consume_auth_rate_limit(
        bucket="auth.login.ip",
        identifier="192.0.2.1",
        limit=4,
        window_seconds=60,
        now_epoch=120,
        postgres_repository=repository,
    )

    with store._connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM auth_rate_limit_buckets").fetchone()[0]
    assert result is None
    assert after == before
    assert repository.calls[0]["bucket_hash"] != "192.0.2.1"
    assert repository.calls[0]["limit"] == 4


def test_auth_route_fails_closed_when_postgres_rate_limit_runtime_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.auth_routes.get_settings",
        lambda: SimpleNamespace(
            postgres_auth_rate_limits_enabled=True,
            auth_rate_limit_window_seconds=60,
        ),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(postgres_runtime=None)))

    with pytest.raises(RuntimeError, match="runtime is unavailable"):
        _limit(request, "auth.login.ip", "192.0.2.2", 4)


def test_auth_route_uses_rate_limit_repository_from_lifespan_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    class FakeRepository:
        calls: list[dict[str, object]] = []

        def consume(self, **kwargs: object) -> int | None:
            self.calls.append(kwargs)
            return None

    repository = FakeRepository()
    monkeypatch.setattr(
        "app.auth_routes.get_settings",
        lambda: SimpleNamespace(
            postgres_auth_rate_limits_enabled=True,
            postgres_auth_enabled=False,
            auth_rate_limit_window_seconds=60,
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                postgres_repositories=SimpleNamespace(auth_rate_limits=repository),
            ),
        ),
    )

    _limit(request, "auth.login.ip", "192.0.2.3", 4)

    assert len(repository.calls) == 1
    assert repository.calls[0]["limit"] == 4


def test_login_endpoint_returns_retry_after_after_ip_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIXION_AUTH_LOGIN_LIMIT_PER_IP", "1")
    payload = {"email": "owner@example.com", "password": "wrong-password"}

    first = client.post("/auth/login", json=payload)
    second = client.post("/auth/login", json=payload)

    assert first.status_code == 401
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= 60


def test_verification_resend_is_limited_per_email_even_across_ip_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_AUTH_RESEND_LIMIT_PER_IP", "100")
    monkeypatch.setenv("AIXION_AUTH_RESEND_LIMIT_PER_EMAIL", "1")
    payload = {"email": "owner@example.com"}

    first = client.post("/auth/resend-verification", json=payload)
    second = client.post("/auth/resend-verification", json=payload)

    assert first.status_code == 404
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= 60


def test_verification_code_guesses_are_limited_per_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIXION_AUTH_VERIFY_LIMIT_PER_IP", "100")
    monkeypatch.setenv("AIXION_AUTH_VERIFY_LIMIT_PER_EMAIL", "1")
    payload = {"email": "owner@example.com", "code": "000000"}

    first = client.post("/auth/verify-email", json=payload)
    second = client.post("/auth/verify-email", json=payload)

    assert first.status_code == 404
    assert second.status_code == 429
