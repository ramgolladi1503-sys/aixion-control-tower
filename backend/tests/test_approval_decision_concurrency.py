from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock
from types import SimpleNamespace

from fastapi import HTTPException

from app import main
from app import agent_routes
from app import approval_expiry
from app.approval_integrity import compute_approval_payload_hash
from app.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    AuthUser,
    DecisionCreate,
    FileChange,
    Notification,
    Project,
    RiskAssessment,
    RiskLevel,
    UserRole,
    now_utc,
)
from app.store import store


def _persist_approval(approval: ApprovalRequest) -> None:
    store.projects[approval.project_id] = Project(
        id=approval.project_id,
        name="Concurrency test project",
        workspace_id=approval.workspace_id,
    )
    store.approval_requests[approval.id] = approval
    store.persist()


def test_deadline_sweep_reads_durable_repository_when_cache_entry_is_missing() -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_missing_approval_cache",
        workspace_id="workspace_missing_approval_cache",
        title="Expire without a compatibility cache entry",
        summary="The durable repository must drive expiry.",
        agent_name="test-agent",
        target_branch="feature/expiry-repository-source",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        expires_at=now_utc() - timedelta(seconds=1),
    )
    _persist_approval(approval)
    store.approval_requests.pop(approval.id)

    assert approval_expiry.expire_due_approvals() == 1
    assert store.approval_requests[approval.id].status == ApprovalStatus.EXPIRED
    persisted = main.approval_decision_repository.get(approval.id)
    assert persisted is not None
    assert persisted.status == ApprovalStatus.EXPIRED
    assert any(
        event.event_type == "approval.expired"
        and event.entity_id == approval.id
        and event.workspace_id == approval.workspace_id
        for event in store.audit_events
    )


def test_simultaneous_approve_and_deny_allow_only_one_decision(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_concurrency",
        title="Review a documentation change",
        summary="Concurrency regression case",
        agent_name="test-agent",
        target_branch="feature/concurrency-test",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        source_session_id="session-concurrency-test",
    )
    _persist_approval(approval)
    user = AuthUser(
        id="reviewer-concurrency",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )

    first_in_transition = Event()
    release_first = Event()
    second_in_transition = Event()
    call_lock = Lock()
    call_count = 0
    original_can_transition = main.can_transition

    def observed_can_transition(previous, target) -> bool:
        nonlocal call_count
        with call_lock:
            call_count += 1
            ordinal = call_count
        if ordinal == 1:
            first_in_transition.set()
            assert release_first.wait(timeout=3)
        elif ordinal == 2:
            second_in_transition.set()
        return original_can_transition(previous, target)

    monkeypatch.setattr(main, "can_transition", observed_can_transition)

    def decide(decision: str):
        return main.decide_approval(
            approval.id,
            DecisionCreate(decision=decision),
            user,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(decide, "approve")
        assert first_in_transition.wait(timeout=3)
        deny_future = executor.submit(decide, "deny")

        try:
            assert not second_in_transition.wait(timeout=0.1), (
                "the competing decision entered transition validation while the first "
                "decision still held the approval critical section"
            )
        finally:
            release_first.set()

        outcomes = []
        for future in (approve_future, deny_future):
            try:
                outcomes.append(future.result(timeout=5))
            except HTTPException as exc:
                outcomes.append(exc)

    assert sum(not isinstance(outcome, HTTPException) for outcome in outcomes) == 1
    assert sum(
        isinstance(outcome, HTTPException) and outcome.status_code == 409
        for outcome in outcomes
    ) == 1
    assert store.approval_requests[approval.id].status in {
        ApprovalStatus.APPROVED,
        ApprovalStatus.DENIED,
    }
    assert store._load_entities("approval_request", ApprovalRequest)[approval.id].status == (
        store.approval_requests[approval.id].status
    )
    assert len(
        [
            event
            for event in store.audit_events
            if event.event_type == "approval.decision" and event.entity_id == approval.id
        ]
    ) == 1


def test_decision_does_not_rewrite_unrelated_durable_records(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_decision_preserves_other_records",
        title="Preserve unrelated durable data",
        summary="Decision commit must not rewrite the whole store.",
        agent_name="test-agent",
        target_branch="feature/decision-preserves-records",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    _persist_approval(approval)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO kv_store(entity_type, entity_id, payload) VALUES (?, ?, ?)",
            ("external_writer_record", "record_from_other_writer", '{"version":1}'),
        )

    def fail_if_full_store_rewrite(**_kwargs) -> None:
        raise AssertionError("approval decision must not rewrite the entire SQLite store")

    monkeypatch.setattr(store, "persist", fail_if_full_store_rewrite)
    reviewer = AuthUser(
        id="reviewer_preserve_durable_records",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )

    result = main.decide_approval(
        approval.id,
        DecisionCreate(decision="deny"),
        reviewer,
    )

    assert result.status == ApprovalStatus.DENIED
    with store._connect() as connection:
        preserved = connection.execute(
            "SELECT payload FROM kv_store WHERE entity_type=? AND entity_id=?",
            ("external_writer_record", "record_from_other_writer"),
        ).fetchone()
    assert preserved == ('{"version":1}',)


def test_agent_poll_cannot_observe_approval_before_state_and_audit_persist(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_durable_decision",
        title="Review a documentation change",
        summary="Durability regression case",
        agent_name="test-agent",
        target_branch="feature/durable-decision-test",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        source_agent_id="external-agent-durable-test",
    )
    _persist_approval(approval)
    reviewer = AuthUser(
        id="reviewer-durable-test",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )
    agent = SimpleNamespace(id=approval.source_agent_id)
    monkeypatch.setattr(agent_routes, "assert_agent_can", lambda *_args, **_kwargs: None)

    persist_started = Event()
    allow_persist = Event()
    persist_finished = Event()
    original_commit = main.approval_decision_repository.commit_decision

    def delayed_commit(**kwargs):
        persist_started.set()
        assert allow_persist.wait(timeout=3)
        task = original_commit(**kwargs)
        persist_finished.set()
        return task

    monkeypatch.setattr(main.approval_decision_repository, "commit_decision", delayed_commit)

    with ThreadPoolExecutor(max_workers=2) as executor:
        decision_future = executor.submit(
            main.decide_approval,
            approval.id,
            DecisionCreate(decision="approve"),
            reviewer,
        )
        assert persist_started.wait(timeout=3)

        poll_started = Event()

        def poll_decision():
            poll_started.set()
            return agent_routes.get_agent_approval_decision(approval.id, agent)

        poll_future = executor.submit(poll_decision)
        assert poll_started.wait(timeout=3)
        assert not poll_future.done(), "agent observed approval before durable commit completed"

        allow_persist.set()
        decision_result = decision_future.result(timeout=5)
        poll_result = poll_future.result(timeout=5)

    assert persist_finished.is_set()
    assert decision_result.status == ApprovalStatus.APPROVED
    assert poll_result.status == ApprovalStatus.APPROVED
    assert len(
        [
            event
            for event in store.audit_events
            if event.event_type == "approval.decision" and event.entity_id == approval.id
        ]
    ) == 1


def test_failed_approval_persist_restores_pending_state_and_blocks_agent(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_failed_commit",
        title="Review a documentation change",
        summary="Failed-commit regression case",
        agent_name="test-agent",
        target_branch="feature/failed-commit-test",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        source_agent_id="external-agent-failed-commit-test",
    )
    _persist_approval(approval)
    reviewer = AuthUser(
        id="reviewer-failed-commit",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )
    agent = SimpleNamespace(id=approval.source_agent_id)
    monkeypatch.setattr(agent_routes, "assert_agent_can", lambda *_args, **_kwargs: None)

    observed_during_commit: list[ApprovalStatus] = []

    def fail_commit(**_kwargs) -> None:
        observed_during_commit.append(
            agent_routes.get_agent_approval_decision(approval.id, agent).status
        )
        assert not any(
            event.event_type == "approval.decision" and event.entity_id == approval.id
            for event in store.audit_events
        )
        raise OSError("simulated durable-store failure")

    monkeypatch.setattr(main.approval_decision_repository, "commit_decision", fail_commit)

    try:
        main.decide_approval(
            approval.id,
            DecisionCreate(decision="approve"),
            reviewer,
        )
    except OSError as exc:
        assert str(exc) == "simulated durable-store failure"
    else:
        raise AssertionError("approval decision unexpectedly succeeded without persistence")

    polled = agent_routes.get_agent_approval_decision(approval.id, agent)
    assert observed_during_commit == [ApprovalStatus.REQUESTED]
    assert polled.status == ApprovalStatus.REQUESTED
    assert polled.approved_payload_hash is None
    assert not any(
        event.event_type == "approval.decision" and event.entity_id == approval.id
        for event in store.audit_events
    )
    durable = store._load_entities("approval_request", ApprovalRequest)[approval.id]
    assert durable.status == ApprovalStatus.REQUESTED
    assert durable.approved_payload_hash is None


def test_agent_poll_reads_decision_committed_outside_local_map(monkeypatch) -> None:
    store.reset()
    assert agent_routes.approval_decision_repository is main.approval_decision_repository
    approval = ApprovalRequest(
        project_id="project_cross_process_poll",
        title="Review a documentation change",
        summary="Poll must use durable decision authority",
        agent_name="test-agent",
        target_branch="feature/cross-process-poll",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        source_agent_id="external-agent-cross-process-poll",
    )
    _persist_approval(approval)
    agent = SimpleNamespace(id=approval.source_agent_id)
    monkeypatch.setattr(agent_routes, "assert_agent_can", lambda *_args, **_kwargs: None)

    approved = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "approved_payload_hash": compute_approval_payload_hash(approval),
        }
    )
    main.approval_decision_repository.commit_decision(
        approval_id=approval.id,
        expected_status=ApprovalStatus.REQUESTED,
        expected_approval=approval,
        updated_approval=approved,
        audit_event=AuditEvent(
            event_type="approval.decision",
            entity_id=approval.id,
            details={
                "previous_status": ApprovalStatus.REQUESTED,
                "new_status": ApprovalStatus.APPROVED,
                "approved_payload_hash": approved.approved_payload_hash,
            },
        ),
        decision_notification=Notification(
            title="Approval approved",
            body="Decision recorded",
            entity_type="approval_request",
            entity_id=approval.id,
        ),
    )

    assert store.approval_requests[approval.id].status == ApprovalStatus.REQUESTED
    polled = agent_routes.get_agent_approval_decision(approval.id, agent)

    assert polled.status == ApprovalStatus.APPROVED
    assert polled.approved_payload_hash == compute_approval_payload_hash(approval)
    assert store.approval_requests[approval.id].status == ApprovalStatus.APPROVED


def test_route_refreshes_durable_state_after_competing_repository_decision(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_repository_conflict",
        title="Review a documentation change",
        summary="Competing durable decision case",
        agent_name="test-agent",
        target_branch="feature/repository-conflict",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
    )
    _persist_approval(approval)
    user = AuthUser(
        id="reviewer-repository-conflict",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )
    original_commit = main.approval_decision_repository.commit_decision

    def competing_commit(**kwargs) -> None:
        concurrent_update = kwargs["updated_approval"].model_copy(
            update={"status": ApprovalStatus.DENIED}
        )
        original_commit(
            approval_id=approval.id,
            expected_status=ApprovalStatus.REQUESTED,
            expected_approval=kwargs["expected_approval"],
            updated_approval=concurrent_update,
            audit_event=AuditEvent(
                id="audit_competing_decision",
                event_type="approval.decision",
                entity_id=approval.id,
                details={
                    "previous_status": ApprovalStatus.REQUESTED,
                    "new_status": ApprovalStatus.DENIED,
                    "approved_payload_hash": concurrent_update.approved_payload_hash,
                },
            ),
            decision_notification=kwargs["decision_notification"],
        )
        original_commit(**kwargs)

    monkeypatch.setattr(
        main.approval_decision_repository,
        "commit_decision",
        competing_commit,
    )

    try:
        main.decide_approval(
            approval.id,
            DecisionCreate(decision="approve"),
            user,
        )
    except HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("route accepted a decision after a competing durable decision")

    assert store.approval_requests[approval.id].status == ApprovalStatus.DENIED
    durable = store._load_entities("approval_request", ApprovalRequest)[approval.id]
    assert durable.status == ApprovalStatus.DENIED
    assert not any(
        event.event_type == "approval.decision"
        and event.entity_id == approval.id
        and event.details.get("new_status") == ApprovalStatus.APPROVED
        for event in store.audit_events
    )


def test_agent_timeout_and_phone_approval_are_serialized(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_timeout_race",
        title="Review a documentation change",
        summary="Timeout-race regression case",
        agent_name="test-agent",
        target_branch="feature/timeout-race-test",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        source_agent_id="external-agent-timeout-race",
    )
    _persist_approval(approval)
    reviewer = AuthUser(
        id="reviewer-timeout-race",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )
    agent = SimpleNamespace(id=approval.source_agent_id)
    monkeypatch.setattr(agent_routes, "assert_agent_can", lambda *_args, **_kwargs: None)

    expiry_started = Event()
    release_expiry = Event()
    approval_transition_started = Event()
    original_can_transition = main.can_transition

    original_expire_pending = main.approval_decision_repository.expire_pending

    def delayed_expire_pending(**kwargs):
        expiry_started.set()
        assert release_expiry.wait(timeout=3)
        return original_expire_pending(**kwargs)

    def observed_can_transition(previous, target) -> bool:
        approval_transition_started.set()
        return original_can_transition(previous, target)

    monkeypatch.setattr(
        main.approval_decision_repository,
        "expire_pending",
        delayed_expire_pending,
    )
    monkeypatch.setattr(main, "can_transition", observed_can_transition)

    with ThreadPoolExecutor(max_workers=2) as executor:
        expire_future = executor.submit(agent_routes.expire_agent_approval, approval.id, agent)
        assert expiry_started.wait(timeout=3)
        approve_future = executor.submit(
            main.decide_approval,
            approval.id,
            DecisionCreate(decision="approve"),
            reviewer,
        )

        try:
            assert not approval_transition_started.wait(timeout=0.1), (
                "phone approval entered transition validation while host expiry held "
                "the approval critical section"
            )
        finally:
            release_expiry.set()

        expired_result = expire_future.result(timeout=5)
        try:
            approve_future.result(timeout=5)
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("phone approval succeeded after the request was expired")

    assert expired_result.status == ApprovalStatus.EXPIRED
    assert store.approval_requests[approval.id].status == ApprovalStatus.EXPIRED


def test_deadline_sweep_and_phone_approval_are_serialized(monkeypatch) -> None:
    store.reset()
    approval = ApprovalRequest(
        project_id="project_deadline_sweep_race",
        title="Review a documentation change",
        summary="Deadline-sweep race regression case",
        agent_name="test-agent",
        target_branch="feature/deadline-sweep-race-test",
        files=[FileChange(path="README.md", diff="+safe change")],
        risk=RiskAssessment(level=RiskLevel.LOW),
        status=ApprovalStatus.REQUESTED,
        expires_at=now_utc() - timedelta(seconds=1),
    )
    _persist_approval(approval)
    reviewer = AuthUser(
        id="reviewer-deadline-sweep-race",
        email="reviewer@example.test",
        display_name="Reviewer",
        role=UserRole.REVIEWER,
    )

    expiry_started = Event()
    release_expiry = Event()
    approval_transition_started = Event()
    original_can_transition = main.can_transition
    original_expire_pending = main.approval_decision_repository.expire_pending

    def delayed_expire_pending(**kwargs):
        expiry_started.set()
        assert release_expiry.wait(timeout=3)
        return original_expire_pending(**kwargs)

    def observed_can_transition(previous, target) -> bool:
        approval_transition_started.set()
        return original_can_transition(previous, target)

    monkeypatch.setattr(
        main.approval_decision_repository,
        "expire_pending",
        delayed_expire_pending,
    )
    monkeypatch.setattr(main, "can_transition", observed_can_transition)

    with ThreadPoolExecutor(max_workers=2) as executor:
        sweep_future = executor.submit(approval_expiry.expire_due_approvals)
        assert expiry_started.wait(timeout=3)
        approve_future = executor.submit(
            main.decide_approval,
            approval.id,
            DecisionCreate(decision="approve"),
            reviewer,
        )

        try:
            assert not approval_transition_started.wait(timeout=0.1), (
                "phone approval entered transition validation while the deadline "
                "sweeper held the approval critical section"
            )
        finally:
            release_expiry.set()

        assert sweep_future.result(timeout=5) == 1
        try:
            approve_future.result(timeout=5)
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("phone approval succeeded after the deadline sweep expired it")

    assert store.approval_requests[approval.id].status == ApprovalStatus.EXPIRED
