from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts import run_postgres_push_dispatch_once as worker


class FakeRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.repositories = SimpleNamespace(notification_outbox=object())
        self.ready = ready
        self.opened = False
        self.closed = False

    def open(self, *, wait: bool = False) -> None:
        self.opened = wait

    def probe(self) -> SimpleNamespace:
        return SimpleNamespace(ready=self.ready)

    def close(self) -> None:
        self.closed = True


def test_disabled_dispatch_does_not_create_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        worker,
        "validate_startup_environment",
        lambda: SimpleNamespace(push_dispatch_enabled=False),
    )

    def unexpected_runtime() -> FakeRuntime:
        raise AssertionError("disabled worker must not create a PostgreSQL runtime")

    assert worker.run_once(runtime_factory=unexpected_runtime) is False


def test_startup_guard_refusal_happens_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_startup() -> SimpleNamespace:
        raise RuntimeError("production persistence guard is active")

    monkeypatch.setattr(worker, "validate_startup_environment", reject_startup)
    runtime_created = False

    def create_runtime() -> FakeRuntime:
        nonlocal runtime_created
        runtime_created = True
        return FakeRuntime()

    with pytest.raises(RuntimeError, match="persistence guard is active"):
        worker.run_once(runtime_factory=create_runtime)

    assert runtime_created is False


def test_inline_mode_refuses_one_shot_worker_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "validate_startup_environment",
        lambda: SimpleNamespace(outbox_dispatch_mode="inline", push_dispatch_enabled=True),
    )

    def unexpected_runtime() -> FakeRuntime:
        raise AssertionError("inline mode must not create a competing scheduled worker")

    with pytest.raises(RuntimeError, match="AIXION_OUTBOX_DISPATCH_MODE=scheduled"):
        worker.run_once(runtime_factory=unexpected_runtime)


def test_ready_dispatch_processes_at_most_one_item_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "validate_startup_environment",
        lambda: SimpleNamespace(outbox_dispatch_mode="scheduled", push_dispatch_enabled=True),
    )
    runtime = FakeRuntime()
    calls: list[object] = []

    def dispatch(repository: object) -> str:
        calls.append(repository)
        return "notification-id"

    assert worker.run_once(runtime_factory=lambda: runtime, dispatcher=dispatch) is True
    assert runtime.opened is True
    assert runtime.closed is True
    assert calls == [runtime.repositories.notification_outbox]


def test_schema_readiness_failure_closes_runtime_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "validate_startup_environment",
        lambda: SimpleNamespace(outbox_dispatch_mode="scheduled", push_dispatch_enabled=True),
    )
    runtime = FakeRuntime(ready=False)
    dispatched = False

    def dispatch(_repository: object) -> str | None:
        nonlocal dispatched
        dispatched = True
        return None

    with pytest.raises(RuntimeError, match="schema is not ready"):
        worker.run_once(runtime_factory=lambda: runtime, dispatcher=dispatch)

    assert runtime.closed is True
    assert dispatched is False


def test_empty_outbox_returns_idle_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "validate_startup_environment",
        lambda: SimpleNamespace(outbox_dispatch_mode="scheduled", push_dispatch_enabled=True),
    )
    runtime = FakeRuntime()
    calls: list[object] = []

    def dispatch(repository: object) -> str | None:
        calls.append(repository)
        return None

    assert worker.run_once(runtime_factory=lambda: runtime, dispatcher=dispatch) is False
    assert runtime.opened is True
    assert runtime.closed is True
    assert calls == [runtime.repositories.notification_outbox]
