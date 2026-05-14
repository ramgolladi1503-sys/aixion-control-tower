from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_EMAIL = "smoke-owner@example.com"
DEFAULT_PASSWORD = "SmokePassword123!"
DEFAULT_REPORT_PATH = Path("docs/release_reports/backend_demo_smoke_summary.md")


@dataclass
class SmokeCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class SmokeValidationSummary:
    base_url: str
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    checks: list[SmokeCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> list[SmokeCheck]:
        return [check for check in self.checks if not check.passed]


def render_smoke_summary_markdown(summary: SmokeValidationSummary) -> str:
    decision = "PASS" if summary.passed else "FAIL"
    rows = "\n".join(
        f"| {check.name} | {'PASS' if check.passed else 'FAIL'} | {check.detail} |"
        for check in summary.checks
    )
    return f"""# Backend Demo Smoke Validation Summary

Generated at: `{summary.generated_at.isoformat()}`

Base URL: `{summary.base_url}`

Smoke decision: **{decision}**

| Check | Result | Detail |
| --- | --- | --- |
{rows}

## Operator note

This smoke summary proves the core demo API flow responds end-to-end. It does not replace full CI,
manual release review, Android validation, incident runbooks, production monitoring, or database backups.
"""


class DemoSmokeValidator:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        email: str = DEFAULT_EMAIL,
        password: str = DEFAULT_PASSWORD,
        token: str = "",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        self.token = token
        self.client = client or httpx.Client(base_url=self.base_url, timeout=timeout_seconds)
        self.summary = SmokeValidationSummary(base_url=self.base_url)

    def close(self) -> None:
        self.client.close()

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _check(self, name: str, passed: bool, detail: str = "") -> bool:
        self.summary.checks.append(SmokeCheck(name=name, passed=passed, detail=detail))
        return passed

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> tuple[httpx.Response | None, str | None]:
        try:
            response = self.client.request(
                method,
                path,
                json=json_payload,
                params=params,
                headers=self._headers() if auth else {},
            )
            return response, None
        except httpx.RequestError as exc:
            return None, str(exc)

    def _response_detail(self, response: httpx.Response | None, error: str | None) -> str:
        if error:
            return error
        if response is None:
            return "no response"
        try:
            body = response.json()
        except Exception:
            body = response.text[:200]
        return f"HTTP {response.status_code}: {body}"

    def _json(self, response: httpx.Response) -> dict[str, Any]:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def validate_health(self) -> bool:
        response, error = self._request("GET", "/health", auth=False)
        passed = response is not None and response.status_code == 200
        detail = "health endpoint reachable" if passed else self._response_detail(response, error)
        return self._check("health", passed, detail)

    def validate_auth_context(self) -> bool:
        response, _ = self._request("GET", "/auth/me", auth=False)
        if response is not None and response.status_code == 200:
            user = self._json(response)
            return self._check("auth_context", True, f"auth bypass/dev user: {user.get('email', 'unknown')}")

        if self.token:
            response, error = self._request("GET", "/auth/me", auth=True)
            passed = response is not None and response.status_code == 200
            detail = "provided bearer token accepted" if passed else self._response_detail(response, error)
            return self._check("auth_context", passed, detail)

        register_payload = {
            "email": self.email,
            "password": self.password,
            "display_name": "Smoke Owner",
        }
        response, error = self._request("POST", "/auth/register", json_payload=register_payload, auth=False)
        if response is not None and response.status_code == 200:
            payload = self._json(response)
            self.token = str(payload.get("access_token", ""))
            return self._check("auth_context", bool(self.token), "bootstrap smoke owner registered")

        login_payload = {"email": self.email, "password": self.password}
        response, error = self._request("POST", "/auth/login", json_payload=login_payload, auth=False)
        if response is not None and response.status_code == 200:
            payload = self._json(response)
            self.token = str(payload.get("access_token", ""))
            return self._check("auth_context", bool(self.token), "existing smoke owner logged in")

        return self._check("auth_context", False, self._response_detail(response, error))

    def validate_readiness(self) -> bool:
        response, error = self._request("GET", "/ops/readiness")
        payload = self._json(response) if response is not None and response.status_code == 200 else {}
        passed = response is not None and response.status_code == 200 and payload.get("status") == "ready"
        detail = f"status={payload.get('status')} profile={payload.get('profile')}" if payload else self._response_detail(response, error)
        return self._check("runtime_readiness", passed, detail)

    def validate_core_demo_flow(self) -> bool:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        project_payload = {
            "name": f"Smoke Demo Project {timestamp}",
            "description": "Generated by backend demo smoke validation.",
            "mode": "STRICT",
            "rules": ["Smoke validation changes require review"],
        }
        response, error = self._request("POST", "/projects", json_payload=project_payload)
        if response is None or response.status_code != 200:
            return self._check("core_demo_flow", False, f"project create failed: {self._response_detail(response, error)}")
        project_id = self._json(response).get("id")

        work_order_payload = {
            "project_id": project_id,
            "goal": "Validate backend demo smoke flow.",
            "context": "Smoke validation should create a project, work order, and approval request.",
            "tasks": ["Create project", "Create work order", "Create approval"],
            "affected_areas": ["backend/demo"],
            "required_tests": ["python -m pytest"],
            "rollback_plan": "Delete smoke data from demo/test environment if needed.",
        }
        response, error = self._request("POST", "/work-orders", json_payload=work_order_payload)
        if response is None or response.status_code != 200:
            return self._check("core_demo_flow", False, f"work order create failed: {self._response_detail(response, error)}")
        work_order_id = self._json(response).get("id")

        approval_payload = {
            "project_id": project_id,
            "work_order_id": work_order_id,
            "title": "Smoke validation approval",
            "summary": "Validates core approval creation for the demo flow.",
            "agent_name": "smoke-validator",
            "target_branch": "feature/smoke-validation",
            "files": [
                {
                    "path": "docs/smoke-validation.md",
                    "change_type": "update",
                    "diff": "+ smoke validation evidence",
                    "new_content": "smoke validation evidence\n",
                }
            ],
            "test_plan": ["python -m pytest"],
            "rollback_plan": "Do not merge the smoke validation branch.",
        }
        response, error = self._request("POST", "/approvals", json_payload=approval_payload)
        if response is None or response.status_code != 200:
            return self._check("core_demo_flow", False, f"approval create failed: {self._response_detail(response, error)}")
        approval_id = self._json(response).get("id")
        return self._check("core_demo_flow", True, f"project={project_id} work_order={work_order_id} approval={approval_id}")

    def validate_audit_export(self) -> bool:
        response, error = self._request("GET", "/audit/export", params={"limit": 10})
        payload = self._json(response) if response is not None and response.status_code == 200 else {}
        passed = response is not None and response.status_code == 200 and "retention_policy" in payload
        detail = f"count={payload.get('count')} truncated={payload.get('truncated')}" if payload else self._response_detail(response, error)
        return self._check("audit_export", passed, detail)

    def validate_recovery_snapshot(self) -> bool:
        response, error = self._request("GET", "/ops/recovery/export")
        if response is None or response.status_code != 200:
            return self._check("recovery_snapshot", False, f"export failed: {self._response_detail(response, error)}")

        snapshot = self._json(response)
        response, error = self._request("POST", "/ops/recovery/validate", json_payload={"snapshot": snapshot})
        payload = self._json(response) if response is not None and response.status_code == 200 else {}
        passed = response is not None and response.status_code == 200 and payload.get("valid") is True
        detail = f"valid={payload.get('valid')} errors={len(payload.get('errors', []))}" if payload else self._response_detail(response, error)
        return self._check("recovery_snapshot", passed, detail)

    def run(self) -> SmokeValidationSummary:
        if not self.validate_health():
            return self.summary
        if not self.validate_auth_context():
            return self.summary
        self.validate_readiness()
        self.validate_core_demo_flow()
        self.validate_audit_export()
        self.validate_recovery_snapshot()
        return self.summary


def write_smoke_summary(path: Path, summary: SmokeValidationSummary) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_smoke_summary_markdown(summary), encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run backend end-to-end demo smoke validation.")
    parser.add_argument("--base-url", default=os.getenv("AIXION_SMOKE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--email", default=os.getenv("AIXION_SMOKE_EMAIL", DEFAULT_EMAIL))
    parser.add_argument("--password", default=os.getenv("AIXION_SMOKE_PASSWORD", DEFAULT_PASSWORD))
    parser.add_argument("--token", default=os.getenv("AIXION_SMOKE_TOKEN", ""))
    parser.add_argument("--output", default=os.getenv("AIXION_SMOKE_OUTPUT", str(DEFAULT_REPORT_PATH)))
    parser.add_argument("--json", action="store_true", help="Print JSON summary instead of markdown.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    validator = DemoSmokeValidator(
        base_url=args.base_url,
        email=args.email,
        password=args.password,
        token=args.token,
    )
    try:
        summary = validator.run()
    finally:
        validator.close()

    output_path = write_smoke_summary(Path(args.output), summary)
    if args.json:
        print(
            json.dumps(
                {
                    "passed": summary.passed,
                    "base_url": summary.base_url,
                    "output": str(output_path),
                    "checks": [check.__dict__ for check in summary.checks],
                },
                indent=2,
            )
        )
    else:
        print(render_smoke_summary_markdown(summary))
        print(f"Wrote smoke summary to {output_path}")

    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
