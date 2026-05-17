from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SAMPLE_PAYLOADS = {
    "chatgpt": Path("docs/samples/chatgpt-actions-bridge-payload.json"),
    "codex": Path("docs/samples/codex-agent-bridge-payload.json"),
}

ClientFactory = Callable[[httpx.Timeout], httpx.Client]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def _clean_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _owner_headers(owner_token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {owner_token.strip()}"} if owner_token else {}


def _load_payload(provider: str, repo_root: Path) -> dict[str, Any]:
    path = SAMPLE_PAYLOADS[provider]
    payload_path = repo_root / path
    with payload_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _get_json(client: httpx.Client, url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    response = client.get(url, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body


def _post_json(client: httpx.Client, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    response = client.post(url, json=payload, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body


def check_health(client: httpx.Client, base_url: str) -> CheckResult:
    status, body = _get_json(client, f"{base_url}/health")
    ok = status == 200 and body.get("status") == "ok"
    return CheckResult(
        name="health",
        ok=ok,
        detail="/health returned ok" if ok else f"/health failed with HTTP {status}",
        data=body,
    )


def check_external_agent_readiness(client: httpx.Client, base_url: str, owner_token: str | None) -> CheckResult:
    status, body = _get_json(client, f"{base_url}/ops/external-agent-readiness", headers=_owner_headers(owner_token))
    ok = status == 200 and body.get("status") == "ready"
    return CheckResult(
        name="external_agent_readiness",
        ok=ok,
        detail="external-agent readiness is ready" if ok else f"external-agent readiness failed with HTTP {status}: {body.get('errors') or body.get('detail')}",
        data=body,
    )


def send_connector_payload(
    client: httpx.Client,
    base_url: str,
    connector_id: str,
    connector_secret: str,
    payload: dict[str, Any],
) -> CheckResult:
    status, body = _post_json(
        client,
        f"{base_url}/connectors/{connector_id}/webhook",
        payload=payload,
        headers={"Authorization": f"Bearer {connector_secret.strip()}"},
    )
    task_id = body.get("task_id")
    ok = status == 200 and body.get("accepted") is True and bool(task_id)
    return CheckResult(
        name="connector_webhook",
        ok=ok,
        detail=f"connector webhook accepted task {task_id}" if ok else f"connector webhook failed with HTTP {status}: {body}",
        data=body,
    )


def verify_task_visibility(client: httpx.Client, base_url: str, owner_token: str | None, task_id: str) -> CheckResult:
    if not owner_token:
        return CheckResult(
            name="task_visibility",
            ok=False,
            detail="AIXION_OWNER_TOKEN is required to verify AgentTask visibility on auth-enabled deployments.",
        )
    status, body = _get_json(client, f"{base_url}/agent/tasks/{task_id}", headers=_owner_headers(owner_token))
    ok = status == 200 and body.get("id") == task_id
    return CheckResult(
        name="task_visibility",
        ok=ok,
        detail=f"AgentTask {task_id} is visible" if ok else f"AgentTask visibility failed with HTTP {status}: {body}",
        data=body,
    )


def _default_client_factory(timeout: httpx.Timeout) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def run_validation(
    args: argparse.Namespace,
    repo_root: Path,
    client_factory: ClientFactory = _default_client_factory,
) -> list[CheckResult]:
    base_url = _clean_base_url(args.base_url or os.getenv("AIXION_BASE_URL", ""))
    if not base_url:
        return [CheckResult(name="configuration", ok=False, detail="AIXION_BASE_URL or --base-url is required.")]

    owner_token = args.owner_token or os.getenv("AIXION_OWNER_TOKEN")
    connector_id = args.connector_id or os.getenv("AIXION_CONNECTOR_ID")
    connector_secret = args.connector_secret or os.getenv("AIXION_CONNECTOR_SECRET")

    results: list[CheckResult] = []
    timeout = httpx.Timeout(args.timeout_seconds)
    with client_factory(timeout) as client:
        results.append(check_health(client, base_url))
        results.append(check_external_agent_readiness(client, base_url, owner_token))

        if args.skip_webhook:
            results.append(CheckResult(name="connector_webhook", ok=True, detail="Skipped by --skip-webhook."))
            return results

        if not connector_id or not connector_secret:
            results.append(
                CheckResult(
                    name="connector_webhook",
                    ok=False,
                    detail="AIXION_CONNECTOR_ID and AIXION_CONNECTOR_SECRET are required unless --skip-webhook is used.",
                )
            )
            return results

        payload = _load_payload(args.provider, repo_root)
        webhook_result = send_connector_payload(client, base_url, connector_id, connector_secret, payload)
        results.append(webhook_result)
        task_id = webhook_result.data.get("task_id")
        if task_id:
            results.append(verify_task_visibility(client, base_url, owner_token, task_id))

    return results


def print_report(results: list[CheckResult]) -> None:
    print(json.dumps({"checks": [result.__dict__ for result in results]}, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a deployed Aixion backend for live external-agent smoke testing.")
    parser.add_argument("--base-url", default=None, help="Public Aixion backend base URL. Defaults to AIXION_BASE_URL.")
    parser.add_argument("--owner-token", default=None, help="Owner/maintainer bearer token. Defaults to AIXION_OWNER_TOKEN.")
    parser.add_argument("--connector-id", default=None, help="Connector id. Defaults to AIXION_CONNECTOR_ID.")
    parser.add_argument("--connector-secret", default=None, help="One-time/active connector secret. Defaults to AIXION_CONNECTOR_SECRET.")
    parser.add_argument("--provider", choices=sorted(SAMPLE_PAYLOADS), default="chatgpt", help="Sample payload provider.")
    parser.add_argument("--skip-webhook", action="store_true", help="Only check health and external-agent readiness.")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    results = run_validation(args, repo_root)
    print_report(results)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())