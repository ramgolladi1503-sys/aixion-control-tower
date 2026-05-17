from __future__ import annotations

from pathlib import Path

import httpx

from scripts.validate_live_external_agent import (
    check_external_agent_readiness,
    check_health,
    run_validation,
)


class Args:
    base_url = "https://api.example.com"
    owner_token = "owner-token"
    connector_id = "connector_1"
    connector_secret = "connector-secret"
    provider = "chatgpt"
    skip_webhook = False
    timeout_seconds = 5.0


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


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


def test_run_validation_can_skip_webhook_after_readiness() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/ops/external-agent-readiness":
            return httpx.Response(200, json={"status": "ready"})
        raise AssertionError(f"Unexpected request {request.url}")

    args = Args()
    args.skip_webhook = True

    original_client = httpx.Client

    def fake_client(*_, **__) -> httpx.Client:
        return _client(handler)

    try:
        httpx.Client = fake_client  # type: ignore[assignment]
        results = run_validation(args, Path(__file__).resolve().parents[1])
    finally:
        httpx.Client = original_client  # type: ignore[assignment]

    assert [result.name for result in results] == ["health", "external_agent_readiness", "connector_webhook"]
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
    original_client = httpx.Client

    def fake_client(*_, **__) -> httpx.Client:
        return _client(handler)

    try:
        httpx.Client = fake_client  # type: ignore[assignment]
        results = run_validation(args, Path(__file__).resolve().parents[1])
    finally:
        httpx.Client = original_client  # type: ignore[assignment]

    assert calls == [
        "/health",
        "/ops/external-agent-readiness",
        "/connectors/connector_1/webhook",
        "/agent/tasks/task_1",
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
    original_client = httpx.Client

    def fake_client(*_, **__) -> httpx.Client:
        return _client(handler)

    try:
        httpx.Client = fake_client  # type: ignore[assignment]
        results = run_validation(args, Path(__file__).resolve().parents[1])
    finally:
        httpx.Client = original_client  # type: ignore[assignment]

    assert results[-1].name == "connector_webhook"
    assert results[-1].ok is False
    assert "AIXION_CONNECTOR_ID" in results[-1].detail
