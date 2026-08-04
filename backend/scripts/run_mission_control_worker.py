from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _retry_is_ready(client: httpx.Client, run: dict[str, Any]) -> bool:
    if run.get("status") != "RETRY_WAIT":
        return True
    response = client.get(f"/agent/runs/{run['id']}")
    response.raise_for_status()
    detail = response.json()
    current_step_id = detail.get("run", {}).get("current_step_id")
    current_step = next(
        (step for step in detail.get("steps", []) if step.get("id") == current_step_id),
        None,
    )
    if current_step is None:
        return False
    next_retry_at = _parse_datetime(current_step.get("next_retry_at"))
    return next_retry_at is None or next_retry_at <= datetime.now(UTC)


def _eligible_runs(client: httpx.Client) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for status in ("SCHEDULED", "RETRY_WAIT"):
        response = client.get("/agent/runs", params={"status": status, "limit": 500})
        response.raise_for_status()
        candidates.extend(response.json())
    ready = [run for run in candidates if _retry_is_ready(client, run)]
    return sorted(
        ready,
        key=lambda run: (run.get("created_at") or "", run.get("id") or ""),
    )


def run_once(
    *,
    client: httpx.Client,
    worker_id: str,
    lease_seconds: int,
    timeout_seconds: int,
) -> int:
    watchdog = client.post("/agent/runs/watchdog/recover-stale")
    watchdog.raise_for_status()

    for run in _eligible_runs(client):
        response = client.post(
            f"/agent/runs/{run['id']}/execute-next",
            json={
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "timeout_seconds": timeout_seconds,
            },
        )
        if response.status_code == 409:
            continue
        response.raise_for_status()
        return 1
    return 0


def build_client(*, base_url: str, access_token: str = "") -> httpx.Client:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    return httpx.Client(
        base_url=base_url.rstrip("/") + "/",
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the stateless Aixion Mission Control scheduler through the backend API. "
            "The API process remains the sole owner of durable store mutation."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AIXION_API_BASE_URL", "http://127.0.0.1:8000"),
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("AIXION_OWNER_TOKEN", ""),
    )
    parser.add_argument("--worker-id", default="mission-control-worker")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    with build_client(base_url=args.base_url, access_token=args.access_token) as client:
        while True:
            processed = run_once(
                client=client,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
                timeout_seconds=args.timeout_seconds,
            )
            if args.once:
                return 0
            if processed == 0:
                time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
