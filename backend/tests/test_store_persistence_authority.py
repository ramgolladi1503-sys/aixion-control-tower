from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.store import store


@pytest.mark.parametrize("method_name", ["persist", "persist_records"])
def test_sqlite_store_writes_are_rejected_when_postgres_auth_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    from app import store as store_module

    monkeypatch.setattr(
        store_module,
        "validate_startup_environment",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )
    monkeypatch.setattr(
        store,
        "_connect",
        lambda: pytest.fail("SQLite connection opened in PostgreSQL-authoritative mode"),
    )

    with pytest.raises(RuntimeError, match="SQLite persistence is disabled"):
        if method_name == "persist":
            store.persist()
        else:
            store.persist_records(inserts=[])


def test_local_store_persistence_remains_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import store as store_module

    monkeypatch.setattr(
        store_module,
        "validate_startup_environment",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=False),
    )

    # An empty append-only transaction exercises the local write boundary
    # without changing application records.
    store.persist_records(inserts=[])
