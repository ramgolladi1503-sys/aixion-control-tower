from __future__ import annotations

from .models import ApprovalRequestCreate, RiskAssessment, RiskLevel, WorkOrderCreate

BLOCKED_BRANCHES = {"main", "master", "production", "release"}
BLOCKED_PATH_MARKERS = (
    ".env",
    "secret",
    "credential",
    "token",
    ".pem",
    ".key",
    "id_rsa",
    "secrets.yaml",
)
CRITICAL_PATH_MARKERS = (
    "execution",
    "broker",
    "order",
    "payment",
    "risk_manager",
    "auth",
    "policy",
)
HIGH_PATH_MARKERS = (
    "database",
    "migration",
    "deploy",
    "workflow",
    "ci",
    "security",
)
LOW_PATH_MARKERS = (
    "readme",
    "docs/",
    "examples/",
)


def assess_approval_request(payload: ApprovalRequestCreate) -> RiskAssessment:
    reasons: list[str] = []
    required_actions: list[str] = []
    highest = RiskLevel.LOW

    branch = payload.target_branch.lower().strip()
    if branch in BLOCKED_BRANCHES:
        return RiskAssessment(
            level=RiskLevel.BLOCKED,
            blocked=True,
            reasons=[f"Direct edits to protected branch '{payload.target_branch}' are blocked."],
            required_actions=["Create a feature branch before requesting approval."],
        )

    if not payload.files:
        return RiskAssessment(
            level=RiskLevel.BLOCKED,
            blocked=True,
            reasons=["Approval request has no file changes."],
            required_actions=["Attach at least one file diff."],
        )

    for file_change in payload.files:
        path = file_change.path.lower()
        if any(marker in path for marker in BLOCKED_PATH_MARKERS):
            return RiskAssessment(
                level=RiskLevel.BLOCKED,
                blocked=True,
                reasons=[f"Sensitive file path is blocked: {file_change.path}"],
                required_actions=["Remove secrets or credential files from the request."],
            )

        if not file_change.diff.strip():
            return RiskAssessment(
                level=RiskLevel.BLOCKED,
                blocked=True,
                reasons=[f"Missing diff for file: {file_change.path}"],
                required_actions=["Provide a visible diff before approval."],
            )

        if any(marker in path for marker in CRITICAL_PATH_MARKERS):
            highest = RiskLevel.CRITICAL
            reasons.append(f"Critical path touched: {file_change.path}")
        elif any(marker in path for marker in HIGH_PATH_MARKERS) and highest != RiskLevel.CRITICAL:
            highest = RiskLevel.HIGH
            reasons.append(f"High-risk path touched: {file_change.path}")
        elif any(marker in path for marker in LOW_PATH_MARKERS):
            reasons.append(f"Low-risk documentation/example path touched: {file_change.path}")
        elif highest == RiskLevel.LOW:
            highest = RiskLevel.MEDIUM
            reasons.append(f"Application code or unknown path touched: {file_change.path}")

    if highest in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        if not payload.test_plan:
            required_actions.append("Add a test plan before approval.")
        if not payload.rollback_plan.strip():
            required_actions.append("Add a rollback plan before approval.")

    if not reasons:
        reasons.append("No risky markers found; defaulted to LOW.")

    return RiskAssessment(
        level=highest,
        blocked=False,
        reasons=reasons,
        required_actions=required_actions,
    )


def assess_work_order(payload: WorkOrderCreate) -> RiskLevel:
    text = " ".join(
        [payload.goal, payload.context, *payload.tasks, *payload.affected_areas]
    ).lower()
    if any(marker in text for marker in ("execution", "broker", "payment", "auth", "security")):
        return RiskLevel.CRITICAL
    if any(marker in text for marker in ("database", "deploy", "policy", "risk")):
        return RiskLevel.HIGH
    if any(marker in text for marker in ("dashboard", "api", "config")):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
