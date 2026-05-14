from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Literal

from .models import (
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalStatus,
    AuditEvent,
    FileChange,
    Idea,
    Notification,
    Project,
    ProjectMode,
    RiskLevel,
    TestRun,
    WorkOrder,
)
from .settings import get_settings

DEMO_PROJECT_ID = "project_demo_aixion_control"
DEMO_IDEA_ID = "idea_demo_mobile_approval"
DEMO_WORK_ORDER_ID = "work_demo_mobile_approval"
DEMO_APPROVAL_ID = "approval_demo_runtime_guard"
DEMO_TEST_RUN_ID = "test_demo_runtime_guard"
DEMO_NOTIFICATION_ID = "notification_demo_runtime_guard"
DEMO_AUDIT_EVENT_ID = "audit_demo_seeded"
SAFE_RESET_PROFILES = {"demo", "test"}


@dataclass(frozen=True)
class DemoDataResult:
    action: Literal["seed", "reset"]
    profile: str
    counts: dict[str, int]
    message: str


def _store():
    from .store import store

    return store


def _counts() -> dict[str, int]:
    store = _store()
    return {
        "projects": len(store.projects),
        "ideas": len(store.ideas),
        "work_orders": len(store.work_orders),
        "approval_requests": len(store.approval_requests),
        "test_runs": len(store.test_runs),
        "notifications": len(store.notifications),
        "audit_events": len(store.audit_events),
    }


def _assert_demo_reset_allowed(profile: str) -> None:
    if profile not in SAFE_RESET_PROFILES:
        allowed = ", ".join(sorted(SAFE_RESET_PROFILES))
        raise RuntimeError(f"Demo reset is allowed only for profiles: {allowed}. Current profile: {profile}")


def seed_demo_data(*, force: bool = False) -> DemoDataResult:
    from .risk_engine import assess_approval_request

    settings = get_settings()
    store = _store()
    if force:
        _assert_demo_reset_allowed(settings.profile)
        store.reset()

    project = Project(
        id=DEMO_PROJECT_ID,
        name="Aixion Demo Control Tower",
        description="Seeded demo project for mobile approval console validation.",
        mode=ProjectMode.STRICT,
        rules=[
            "Mutating agent work requires mobile approval.",
            "Risky execution must include tests and rollback plan.",
            "Secrets must never be exposed in readiness or audit views.",
        ],
    )
    store.projects[project.id] = project

    idea = Idea(
        id=DEMO_IDEA_ID,
        project_id=project.id,
        title="Add runtime guard visibility",
        raw_text="Expose backend readiness to the mobile operator before approving risky work.",
    )
    store.ideas[idea.id] = idea

    work_order = WorkOrder(
        id=DEMO_WORK_ORDER_ID,
        project_id=project.id,
        idea_id=idea.id,
        goal="Validate mobile-controlled approval before backend execution.",
        context="This seeded work order shows why a human operator must approve risky agent work.",
        tasks=[
            "Inspect backend readiness.",
            "Review proposed file change.",
            "Confirm test plan and rollback plan.",
            "Approve or deny from Android.",
        ],
        affected_areas=["backend/runtime", "android/operator-ui"],
        required_tests=[
            "python -m pytest",
            "./gradlew test assembleDebug",
        ],
        rollback_plan="Reject the approval or revert the feature branch before execution.",
        risk_level=RiskLevel.HIGH,
    )
    store.work_orders[work_order.id] = work_order

    approval_payload = ApprovalRequestCreate(
        project_id=project.id,
        work_order_id=work_order.id,
        title="Demo: approve runtime guard visibility",
        summary="Seeded approval request showing mobile review before risky backend/operator changes.",
        agent_name="demo-builder-agent",
        target_branch="feature/demo-runtime-guard",
        files=[
            FileChange(
                path="docs/demo/runtime-readiness.md",
                change_type="update",
                diff="+ Document runtime readiness checks for demo operator flow.",
                new_content="Runtime readiness must be checked before risky agent execution.\n",
            )
        ],
        test_plan=[
            "python -m pytest",
            "./gradlew test assembleDebug",
        ],
        rollback_plan="Deny the approval or revert demo-runtime-guard branch.",
    )
    approval = ApprovalRequest(
        id=DEMO_APPROVAL_ID,
        **approval_payload.model_dump(),
        risk=assess_approval_request(approval_payload),
        status=ApprovalStatus.REQUESTED,
        created_by_user_id="demo_seed",
        verified_source=True,
    )
    store.approval_requests[approval.id] = approval

    test_run = TestRun(
        id=DEMO_TEST_RUN_ID,
        approval_request_id=approval.id,
        command="python -m pytest && ./gradlew test assembleDebug",
        status="PENDING",
        output_summary="Seeded demo test evidence placeholder.",
    )
    store.test_runs[test_run.id] = test_run

    notification = Notification(
        id=DEMO_NOTIFICATION_ID,
        title="Demo approval ready",
        body="A seeded runtime guard approval is waiting for mobile review.",
        entity_type="approval_request",
        entity_id=approval.id,
        user_id=None,
    )
    store.notifications[notification.id] = notification

    store.audit_events = [event for event in store.audit_events if event.id != DEMO_AUDIT_EVENT_ID]
    store.audit_events.append(
        AuditEvent(
            id=DEMO_AUDIT_EVENT_ID,
            event_type="demo.seeded",
            actor="demo-seed",
            entity_id=project.id,
            details={
                "project_id": project.id,
                "approval_request_id": approval.id,
                "safe_demo_data": True,
            },
        )
    )

    store.persist()
    return DemoDataResult(
        action="seed",
        profile=settings.profile,
        counts=_counts(),
        message="Seeded deterministic demo data.",
    )


def reset_demo_data() -> DemoDataResult:
    settings = get_settings()
    _assert_demo_reset_allowed(settings.profile)
    store = _store()
    store.reset()
    return DemoDataResult(
        action="reset",
        profile=settings.profile,
        counts=_counts(),
        message="Reset demo/test data store.",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed or reset deterministic Aixion demo data.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed_parser = subparsers.add_parser("seed", help="Seed deterministic demo data.")
    seed_parser.add_argument("--force", action="store_true", help="Reset first. Allowed only in demo/test profiles.")
    subparsers.add_parser("reset", help="Reset all store data. Allowed only in demo/test profiles.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = reset_demo_data() if args.command == "reset" else seed_demo_data(force=args.force)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"{result.action.upper()} OK profile={result.profile} message={result.message}")
    for key, value in sorted(result.counts.items()):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
