from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scripts import run_postgres_email_dispatch_once as worker


class FakeRuntime:
    def __init__(self, *, ready: bool = True) -> None:
        self.repositories = SimpleNamespace(email_outbox=object())
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
        "validate_outbox_worker_environment",
        lambda _worker_kind: SimpleNamespace(email_dispatch_enabled=False),
    )

    def unexpected_runtime() -> FakeRuntime:
        raise AssertionError("disabled worker must not create a PostgreSQL runtime")

    assert worker.run_once(runtime_factory=unexpected_runtime) is False


def test_startup_guard_refusal_happens_before_runtime_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_startup(_worker_kind: str) -> SimpleNamespace:
        raise RuntimeError("production persistence guard is active")

    monkeypatch.setattr(worker, "validate_outbox_worker_environment", reject_startup)
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
        "validate_outbox_worker_environment",
        lambda _worker_kind: SimpleNamespace(outbox_dispatch_mode="inline", email_dispatch_enabled=True),
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
        "validate_outbox_worker_environment",
        lambda _worker_kind: SimpleNamespace(outbox_dispatch_mode="scheduled", email_dispatch_enabled=True),
    )
    runtime = FakeRuntime()
    calls: list[object] = []

    def dispatch(repository: object) -> bool:
        calls.append(repository)
        return True

    assert worker.run_once(runtime_factory=lambda: runtime, dispatcher=dispatch) is True
    assert runtime.opened is True
    assert runtime.closed is True
    assert calls == [runtime.repositories.email_outbox]


def test_schema_readiness_failure_closes_runtime_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "validate_outbox_worker_environment",
        lambda _worker_kind: SimpleNamespace(outbox_dispatch_mode="scheduled", email_dispatch_enabled=True),
    )
    runtime = FakeRuntime(ready=False)
    dispatched = False

    def dispatch(_repository: object) -> bool:
        nonlocal dispatched
        dispatched = True
        return True

    with pytest.raises(RuntimeError, match="schema is not ready"):
        worker.run_once(runtime_factory=lambda: runtime, dispatcher=dispatch)

    assert runtime.closed is True
    assert dispatched is False


def test_empty_outbox_returns_idle_and_closes_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "validate_outbox_worker_environment",
        lambda _worker_kind: SimpleNamespace(outbox_dispatch_mode="scheduled", email_dispatch_enabled=True),
    )
    runtime = FakeRuntime()
    calls: list[object] = []

    def dispatch(repository: object) -> bool:
        calls.append(repository)
        return False

    assert worker.run_once(runtime_factory=lambda: runtime, dispatcher=dispatch) is False
    assert runtime.opened is True
    assert runtime.closed is True
    assert calls == [runtime.repositories.email_outbox]


def test_cli_failure_output_does_not_expose_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda: (_ for _ in ()).throw(RuntimeError("smtp password recipient code")),
    )

    assert worker.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PostgreSQL email dispatch failed" in captured.err
    assert "smtp password recipient code" not in captured.err


def test_cli_success_emits_only_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(worker, "run_once", lambda: True)

    assert worker.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "processed"}
