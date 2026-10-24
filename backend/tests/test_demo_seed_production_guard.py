from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main


def test_demo_seed_is_rejected_in_production_without_mutating_store(monkeypatch) -> None:
    before = {
        name: copy.deepcopy(value)
        for name, value in vars(main.store).items()
        if isinstance(value, (dict, list))
    }
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(is_production=True),
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
