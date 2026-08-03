from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from app.agent_run_models import AgentRunStatus
from app.agent_run_supervisor import AgentRunConflict, execute_next_step, recover_stale_leases
from app.store import store


def _eligible_run_ids() -> list[str]:
    now = datetime.now(timezone.utc)
    eligible: list[tuple[datetime, str]] = []
    for run in store.agent_runs.values():
        if run.status == AgentRunStatus.SCHEDULED:
            eligible.append((run.created_at, run.id))
            continue
        if run.status == AgentRunStatus.RETRY_WAIT:
            step = store.agent_run_steps.get(run.current_step_id or "")
            if step is not None and (step.next_retry_at is None or step.next_retry_at <= now):
                eligible.append((run.created_at, run.id))
    return [run_id for _, run_id in sorted(eligible)]


def run_once(*, worker_id: str, lease_seconds: int, timeout_seconds: int) -> int:
    recover_stale_leases(actor=f"{worker_id}:watchdog")
    for run_id in _eligible_run_ids():
        try:
            execute_next_step(
                run_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                timeout_seconds=timeout_seconds,
            )
            return 1
        except AgentRunConflict:
            continue
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the durable Aixion Mission Control worker.")
    parser.add_argument("--worker-id", default="mission-control-worker")
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    while True:
        processed = run_once(
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
