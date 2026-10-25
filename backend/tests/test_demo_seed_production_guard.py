from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main


@pytest.mark.parametrize(
    ("is_production", "postgres_auth_enabled"),
    [(True, False), (False, True)],
    ids=["production", "postgres-auth"],
)
def test_demo_seed_is_rejected_in_authoritative_modes_without_mutating_store(
    monkeypatch,
    is_production: bool,
    postgres_auth_enabled: bool,
) -> None:
    before = {
        name: copy.deepcopy(value)
        for name, value in vars(main.store).items()
        if isinstance(value, (dict, list))
    }
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            is_production=is_production,
            postgres_auth_enabled=postgres_auth_enabled,
        ),
    )

    with pytest.raises(HTTPException) as raised:
        main.seed_demo_data(None)

    assert raised.value.status_code == 404
    after = {
        name: value
        for name, value in vars(main.store).items()
        if isinstance(value, (dict, list))
    }
    assert after == before


def test_demo_seed_postgres_auth_rejects_before_reading_local_store(
    monkeypatch,
) -> None:
    class ReadTrap(dict):
        def __bool__(self):
            pytest.fail("PostgreSQL-auth demo seed must not inspect SQLite projects")

    monkeypatch.setattr(main.store, "projects", ReadTrap())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(is_production=False, postgres_auth_enabled=True),
    )

    with pytest.raises(HTTPException) as raised:
        main.seed_demo_data(None)

    assert raised.value.status_code == 404
