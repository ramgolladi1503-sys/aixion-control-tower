from __future__ import annotations

from app.postgres_external_agent_auth_repository import (
    PostgresExternalAgentAuthRepository,
)
from app.postgres_runtime import PostgresRuntime


def test_runtime_external_agent_auth_uses_shared_connection_provider(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def connector(dsn: str, **kwargs: object) -> object:
        seen["dsn"] = dsn
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("AIXION_PROFILE", "test")
    monkeypatch.setenv(
        "AIXION_POSTGRES_DSN",
        "postgresql://runtime-test@localhost/aixion_test",
    )
    runtime = PostgresRuntime.from_environment(connector=connector)  # type: ignore[arg-type]

    repository = runtime.external_agent_auth

    assert isinstance(repository, PostgresExternalAgentAuthRepository)
    assert repository._connect() is not None
    assert seen["dsn"] == "postgresql://runtime-test@localhost/aixion_test"
    assert isinstance(seen["kwargs"], dict)
