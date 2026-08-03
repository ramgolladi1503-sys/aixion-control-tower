from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import httpx

os.environ.setdefault("AIXION_AUTH_ENABLED", "false")

from scripts.run_mission_control_worker import build_client, run_once


def _json_response(payload: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(payload).encode("utf-8"),
    )


def test_worker_uses_api_and_executes_one_scheduled_run() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/agent/runs/watchdog/recover-stale":
            return _json_response({"recovered": 0})
        if request.url.path == "/agent/runs" and request.url.params.get("status") == "SCHEDULED":
            return _json_response(
                [
                    {
                        "id": "run_1",
                        "status": "SCHEDULED",
                        "created_at": "2026-08-03T10:00:00+00:00",
                    }
                ]
            )
        if request.url.path == "/agent/runs" and request.url.params.get("status") in {
            "RUNNING",
            "RETRY_WAIT",
        }:
            return _json_response([])
        if request.url.path == "/agent/runs/run_1/execute-next":
            body = json.loads(request.content.decode("utf-8"))
            assert body["worker_id"] == "worker-1"
            assert body["lease_seconds"] == 120
            assert body["timeout_seconds"] == 60
            return _json_response(
                {
                    "run": {"id": "run_1", "status": "RUNNING"},
                    "steps": [],
                    "events": [],
                }
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(
        base_url="http://aixion.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        processed = run_once(
            client=client,
            worker_id="worker-1",
            lease_seconds=120,
            timeout_seconds=60,
        )

    assert processed == 1
    assert ("POST", "/agent/runs/watchdog/recover-stale") in calls
    assert ("POST", "/agent/runs/run_1/execute-next") in calls


def test_worker_continues_run_at_next_durable_step_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/agent/runs/watchdog/recover-stale":
            return _json_response({"recovered": 0})
        if request.url.path == "/agent/runs" and request.url.params.get("status") == "SCHEDULED":
            return _json_response([])
        if request.url.path == "/agent/runs" and request.url.params.get("status") == "RUNNING":
            return _json_response(
                [
                    {
                        "id": "run_continuation",
                        "status": "RUNNING",
                        "created_at": "2026-08-03T10:00:00+00:00",
                    }
                ]
            )
        if request.url.path == "/agent/runs" and request.url.params.get("status") == "RETRY_WAIT":
            return _json_response([])
        if request.url.path == "/agent/runs/run_continuation/execute-next":
            return _json_response(
                {
                    "run": {"id": "run_continuation", "status": "RUNNING"},
                    "steps": [],
                    "events": [],
                }
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(
        base_url="http://aixion.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        processed = run_once(
            client=client,
            worker_id="worker-1",
            lease_seconds=120,
            timeout_seconds=60,
        )

    assert processed == 1


def test_worker_does_not_spin_on_retry_before_backoff_expires() -> None:
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    execute_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal execute_calls
        if request.url.path == "/agent/runs/watchdog/recover-stale":
            return _json_response({"recovered": 0})
        if request.url.path == "/agent/runs" and request.url.params.get("status") in {
            "SCHEDULED",
            "RUNNING",
        }:
            return _json_response([])
        if request.url.path == "/agent/runs" and request.url.params.get("status") == "RETRY_WAIT":
            return _json_response(
                [
                    {
                        "id": "run_retry",
                        "status": "RETRY_WAIT",
                        "created_at": "2026-08-03T10:00:00+00:00",
                    }
                ]
            )
        if request.url.path == "/agent/runs/run_retry":
            return _json_response(
                {
                    "run": {
                        "id": "run_retry",
                        "status": "RETRY_WAIT",
                        "current_step_id": "step_retry",
                    },
                    "steps": [
                        {
                            "id": "step_retry",
                            "status": "RETRY_WAIT",
                            "next_retry_at": future,
                        }
                    ],
                    "events": [],
                }
            )
        if request.url.path.endswith("/execute-next"):
            execute_calls += 1
            return _json_response({}, status_code=500)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with httpx.Client(
        base_url="http://aixion.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        processed = run_once(
            client=client,
            worker_id="worker-1",
            lease_seconds=120,
            timeout_seconds=60,
        )

    assert processed == 0
    assert execute_calls == 0


def test_client_adds_bearer_token_without_exposing_it_in_url() -> None:
    client = build_client(
        base_url="https://aixion.example",
        access_token="secret-token",
    )
    try:
        request = client.build_request("GET", "/agent/runs")
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert "secret-token" not in str(request.url)
    finally:
        client.close()
