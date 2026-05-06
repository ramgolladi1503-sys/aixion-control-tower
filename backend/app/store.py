from __future__ import annotations

from .models import ApprovalRequest, AuditEvent, Idea, Project, TestRun, WorkOrder


class InMemoryStore:
    def __init__(self) -> None:
        self.projects: dict[str, Project] = {}
        self.ideas: dict[str, Idea] = {}
        self.work_orders: dict[str, WorkOrder] = {}
        self.approval_requests: dict[str, ApprovalRequest] = {}
        self.test_runs: dict[str, TestRun] = {}
        self.audit_events: list[AuditEvent] = []

    def reset(self) -> None:
        self.projects.clear()
        self.ideas.clear()
        self.work_orders.clear()
        self.approval_requests.clear()
        self.test_runs.clear()
        self.audit_events.clear()


store = InMemoryStore()
