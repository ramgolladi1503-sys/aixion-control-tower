from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from app.demo_smoke_validation import (
    DemoSmokeValidator,
    SmokeCheck,
    SmokeValidationSummary,
    render_smoke_summary_markdown,
    write_smoke_summary,
)


def _json_response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload)


def test_demo_smoke_validator_passes_core_flow_with_auth_disabled() -> None:
    created: dict[str, int] = {"projects": 0, "work_orders": 0, "approvals": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/health":
            return _json_response(200, {"status": "ok", "service": "aixion-control-tower"})
        if request.method == "GET" and request.url.path == "/auth/me":
            return _json_response(200, {"id": "dev_user", "email": "dev@local", "display_name": "Local Dev", "role": "OWNER"})
        if request.method == "GET" and request.url.path == "/ops/readiness":
            return _json_response(200, {"status": "ready", "profile": "demo"})
        if request.method == "POST" and request.url.path == "/projects":
            created["projects"] += 1
            return _json_response(200, {"id": "project_smoke"})
        if request.method == "POST" and request.url.path == "/work-orders":
            created["work_orders"] += 1
            return _json_response(200, {"id": "work_smoke"})
        if request.method == "POST" and request.url.path == "/approvals":
            created["approvals"] += 1
            return _json_response(200, {"id": "approval_smoke"})
        if request.method == "GET" and request.url.path == "/audit/export":
            return _json_response(200, {"count": 1, "truncated": False, "retention_policy": {"policy_version": "v1"}})
        if request.method == "GET" and request.url.path == "/ops/recovery/export":
            return _json_response(200, {"format_version": "aixion-control-tower-recovery-v1", "entities": {}})
        if request.method == "POST" and request.url.path == "/ops/recovery/validate":
            return _json_response(200, {"valid": True, "errors": []})
        return _json_response(404, {"detail": "missing"})

    client = httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(handler))
    validator = DemoSmokeValidator(base_url="http://testserver", client=client)

    summary = validator.run()

    assert summary.passed is True
    assert [check.name for check in summary.checks] == [
        "health",
        "auth_context",
        "runtime_readiness",
        "core_demo_flow",
        "audit_export",
        "recovery_snapshot",
    ]
    assert created == {"projects": 1, "work_orders": 1, "approvals": 1}


def test_demo_smoke_validator_can_bootstrap_owner_when_auth_enabled() -> None:
    seen_auth_header: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/health":
            return _json_response(200, {"status": "ok"})
        if request.method == "GET" and request.url.path == "/auth/me":
            auth_header = request.headers.get("authorization", "")
            if auth_header:
                seen_auth_header.append(auth_header)
                return _json_response(200, {"email": "smoke-owner@example.com"})
            return _json_response(401, {"detail": "Missing bearer token"})
        if request.method == "POST" and request.url.path == "/auth/register":
            return _json_response(200, {"access_token": "smoke-token", "user": {"email": "smoke-owner@example.com"}})
        if request.method == "GET" and request.url.path == "/ops/readiness":
            assert request.headers.get("authorization") == "Bearer smoke-token"
            return _json_response(200, {"status": "ready", "profile": "local"})
        if request.method == "POST" and request.url.path == "/projects":
            return _json_response(200, {"id": "project_smoke"})
        if request.method == "POST" and request.url.path == "/work-orders":
            return _json_response(200, {"id": "work_smoke"})
        if request.method == "POST" and request.url.path == "/approvals":
            return _json_response(200, {"id": "approval_smoke"})
        if request.method == "GET" and request.url.path == "/audit/export":
            return _json_response(200, {"count": 0, "truncated": False, "retention_policy": {}})
        if request.method == "GET" and request.url.path == "/ops/recovery/export":
            return _json_response(200, {"format_version": "aixion-control-tower-recovery-v1", "entities": {}})
        if request.method == "POST" and request.url.path == "/ops/recovery/validate":
            return _json_response(200, {"valid": True, "errors": []})
        return _json_response(404, {"detail": "missing"})

    client = httpx.Client(base_url="http://testserver", transport=httpx.MockTransport(handler))
    validator = DemoSmokeValidator(base_url="http://testserver", client=client)

    summary = validator.run()

    assert summary.passed is True
    assert validator.token == "smoke-token"


def test_demo_smoke_summary_markdown_and_write(tmp_path: Path) -> None:
    summary = SmokeValidationSummary(base_url="http://testserver")
    summary.checks.append(SmokeCheck("health", True, "ok"))
    summary.checks.append(SmokeCheck("readiness", False, "not ready"))

    markdown = render_smoke_summary_markdown(summary)
    output_path = write_smoke_summary(tmp_path / "smoke.md", summary)

    assert "Smoke decision: **FAIL**" in markdown
    assert "| health | PASS | ok |" in markdown
    assert "| readiness | FAIL | not ready |" in markdown
    assert output_path.exists()
    assert "Backend Demo Smoke Validation Summary" in output_path.read_text(encoding="utf-8")


def test_demo_smoke_json_payload_is_serializable() -> None:
    summary = SmokeValidationSummary(base_url="http://testserver")
    summary.checks.append(SmokeCheck("health", True, "ok"))

    payload = {
        "passed": summary.passed,
        "base_url": summary.base_url,
        "checks": [check.__dict__ for check in summary.checks],
    }

    assert json.loads(json.dumps(payload))["passed"] is True
