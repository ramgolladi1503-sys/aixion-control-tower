from __future__ import annotations

from pathlib import Path

import httpx

from scripts.validate_live_external_agent import (
    check_external_agent_readiness,
    check_health,
    check_phase0_lan_readiness,
    run_validation,
)


class Args:
    base_url = "https://api.example.com"
    owner_token = "owner-token"
    connector_id = "connector_1"
    connector_secret = "connector-secret"
    provider = "chatgpt"
    mode = "public-external"
    skip_webhook = False
    timeout_seconds = 5.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_factory(handler):
    def factory(_timeout: httpx.Timeout) -> httpx.Client:
        return _client(handler)

    return factory


def test_check_health_passes_when_backend_reports_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "service": "aixion-control-tower"})

    with _client(handler) as client:
        result = check_health(client, "https://api.example.com")

    assert result.ok is True
    assert result.name == "health"


def test_external_agent_readiness_requires_ready_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ops/external-agent-readiness"
        assert request.headers["authorization"] == "Bearer owner-token"
        return httpx.Response(200, json={"status": "not_ready", "errors": ["missing public url"]})

    with _client(handler) as client:
        result = check_external_agent_readiness(client, "https://api.example.com", "owner-token")

    assert result.ok is False
    assert "missing public url" in result.detail


def test_phase0_lan_readiness_uses_runtime_readiness_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/ops/readiness"
        assert request.headers["authorization"] == "Bearer owner-token"
        return httpx.Response(200, json={"status": "ready"})

    with _client(handler) as client:
        result = check_phase0_lan_readiness(client, "http://192.168.1.20:8000", "owner-token")

    assert result.ok is True
    assert result.name == "phase0_lan_readiness"


def test_run_validation_can_skip_webhook_after_public_external_readiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ops/external-agent-readiness":
            return httpx.Response(200, json={"status": "ready"})
        raise AssertionError(f"Unexpected request {request.url}")

    args = Args()
    args.skip_webhook = True

    results = run_validation(args, _repo_root(), client_factory=_client_factory(handler))

    assert [result.name for result in results] == ["health", "external_agent_readiness", "connector_webhook"]
    assert all(result.ok for result in results)


def test_run_validation_can_skip_webhook_after_phase0_lan_readiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ops/readiness":
            return httpx.Response(200, json={"status": "ready"})
        raise AssertionError(f"Unexpected request {request.url}")

    args = Args()
    args.mode = "phase0-lan"
    args.base_url = "http://192.168.1.20:8000"
    args.skip_webhook = True

    results = run_validation(args, _repo_root(), client_factory=_client_factory(handler))

    assert [result.name for result in results] == ["health", "phase0_lan_readiness", "connector_webhook"]
    assert all(result.ok for result in results)


def test_run_validation_sends_webhook_and_verifies_task_visibility() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ops/external-agent-readiness":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/connectors/connector_1/webhook":
            assert request.headers["authorization"] == "Bearer connector-secret"
            return httpx.Response(200, json={"accepted": True, "task_id": "task_1"})
        if request.url.path == "/agent/tasks/task_1":
            assert request.headers["authorization"] == "Bearer owner-token"
            return httpx.Response(200, json={"id": "task_1", "provider": "CHATGPT"})
        raise AssertionError(f"Unexpected request {request.url}")

    args = Args()
    args.skip_webhook = False

    results = run_validation(args, _repo_root(), client_factory=_client_factory(handler))

    assert calls == [
        "/health",
        "/ops/external-agent-readiness",
        "/connectors/connector_1/webhook",
        "/agent/tasks/task_1",
    ]
    assert all(result.ok for result in results)


def test_run_validation_sends_phase0_lan_webhook_after_runtime_readiness() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ops/readiness":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path == "/connectors/connector_1/webhook":
            return httpx.Response(200, json={"accepted": True, "task_id": "task_lan"})
        if request.url.path == "/agent/tasks/task_lan":
            return httpx.Response(200, json={"id": "task_lan", "provider": "CHATGPT"})
        raise AssertionError(f"Unexpected request {request.url}")

    args = Args()
    args.mode = "phase0-lan"
    args.base_url = "http://192.168.1.20:8000"

    results = run_validation(args, _repo_root(), client_factory=_client_factory(handler))

    assert calls == [
        "/health",
        "/ops/readiness",
        "/connectors/connector_1/webhook",
        "/agent/tasks/task_lan",
    ]
    assert all(result.ok for result in results)


def test_run_validation_fails_without_connector_credentials_when_webhook_not_skipped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ops/external-agent-readiness":
            return httpx.Response(200, json={"status": "ready"})
        raise AssertionError(f"Unexpected request {request.url}")

    args = Args()
    args.connector_id = None
    args.connector_secret = None

    results = run_validation(args, _repo_root(), client_factory=_client_factory(handler))

    assert results[-1].name == "connector_webhook"
    assert results[-1].ok is False
    assert "AIXION_CONNECTOR_ID" in results[-1].detail
