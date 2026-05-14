from __future__ import annotations

import os

import pytest

os.environ.setdefault("AIXION_PROFILE", "test")
os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from app.demo_data import (
    DEMO_APPROVAL_ID,
    DEMO_PROJECT_ID,
    DemoDataResult,
    _assert_demo_reset_allowed,
    reset_demo_data,
    seed_demo_data,
)
from app.models import ApprovalStatus
from app.store import store


def setup_function() -> None:
    store.reset()


def test_seed_demo_data_creates_deterministic_demo_entities() -> None:
    result = seed_demo_data()

    assert isinstance(result, DemoDataResult)
    assert result.action == "seed"
    assert result.counts["projects"] == 1
    assert result.counts["ideas"] == 1
    assert result.counts["work_orders"] == 1
    assert result.counts["approval_requests"] == 1
    assert result.counts["test_runs"] == 1
    assert result.counts["notifications"] == 1
    assert result.counts["audit_events"] == 1
    assert DEMO_PROJECT_ID in store.projects
    assert DEMO_APPROVAL_ID in store.approval_requests
    assert store.approval_requests[DEMO_APPROVAL_ID].status == ApprovalStatus.REQUESTED
    assert store.approval_requests[DEMO_APPROVAL_ID].verified_source is True


def test_seed_demo_data_is_idempotent() -> None:
    first = seed_demo_data()
    second = seed_demo_data()

    assert first.counts == second.counts
    assert len(store.projects) == 1
    assert len(store.approval_requests) == 1
    assert len([event for event in store.audit_events if event.event_type == "demo.seeded"]) == 1


def test_seed_demo_data_force_resets_before_seeding_in_test_profile() -> None:
    seed_demo_data()
    store.projects["project_extra"] = store.projects[DEMO_PROJECT_ID].model_copy(update={"id": "project_extra"})
    store.persist()

    result = seed_demo_data(force=True)

    assert result.counts["projects"] == 1
    assert list(store.projects) == [DEMO_PROJECT_ID]


def test_reset_demo_data_is_allowed_in_test_profile() -> None:
    seed_demo_data()

    result = reset_demo_data()

    assert result.action == "reset"
    assert result.counts["projects"] == 0
    assert result.counts["approval_requests"] == 0
    assert store.projects == {}
    assert store.approval_requests == {}


def test_reset_guard_rejects_non_demo_test_profiles() -> None:
    with pytest.raises(RuntimeError, match="allowed only for profiles"):
        _assert_demo_reset_allowed("production")

    with pytest.raises(RuntimeError, match="allowed only for profiles"):
        _assert_demo_reset_allowed("local")


def test_reset_guard_allows_demo_and_test_profiles() -> None:
    _assert_demo_reset_allowed("demo")
    _assert_demo_reset_allowed("test")
