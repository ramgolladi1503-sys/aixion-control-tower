from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from psycopg import Connection

from .agent_run_repository import PostgresAgentRunRepository
from .agent_task_repository import PostgresAgentTaskRepository
from .approval_repository import PostgresApprovalDecisionRepository
from .connector_repository import PostgresConnectorRepository
from .notification_outbox_repository import PostgresNotificationOutboxRepository
from .postgres_audit_repository import PostgresAuditEventRepository
from .postgres_auth_rate_limits import PostgresAuthRateLimitRepository
from .postgres_auth_repository import PostgresAuthRepository
from .postgres_email_outbox_repository import PostgresEmailOutboxRepository
from .postgres_external_agent_auth_repository import PostgresExternalAgentAuthRepository
from .idea_repository import PostgresIdeaRepository
from .mcp_child_server_repository import PostgresMCPChildServerRepository
from .mcp_pending_repository import PostgresMCPPendingRepository
from .project_repository import PostgresProjectRepository
from .work_order_repository import PostgresWorkOrderRepository
from .workspace_membership_repository import PostgresWorkspaceMembershipRepository
from .test_run_repository import PostgresTestRunRepository
from .postgres_trust_event_repository import PostgresTrustEventRepository
from .postgres_trust_repository import PostgresTrustRepository
from .postgres_agent_run_branch import PostgresAgentRunBranchIntentRepository
from .postgres_agent_run_apply_changes import PostgresAgentRunApplyChangeIntentRepository
from .postgres_agent_run_validation import PostgresAgentRunValidationIntentRepository
from .postgres_agent_run_pull_request import PostgresAgentRunPullRequestIntentRepository
from .recovery_repository import PostgresRecoverySnapshotRepository


@dataclass(frozen=True)
class ApplicationRepositories:
    """Immutable PostgreSQL adapters sharing one runtime connection factory.

    The application lifespan creates this bundle once and stores it on app
    state. Opening PostgreSQL for diagnostics does not itself select these
    repositories for routes; each domain must opt into the bundle explicitly.
    """

    auth: PostgresAuthRepository
    auth_rate_limits: PostgresAuthRateLimitRepository
    email_outbox: PostgresEmailOutboxRepository
    notification_outbox: PostgresNotificationOutboxRepository
    approval_decisions: PostgresApprovalDecisionRepository
    agent_tasks: PostgresAgentTaskRepository
    projects: PostgresProjectRepository
    agent_runs: PostgresAgentRunRepository
    workspace_membership: PostgresWorkspaceMembershipRepository
    external_agent_auth: PostgresExternalAgentAuthRepository
    work_orders: PostgresWorkOrderRepository
    ideas: PostgresIdeaRepository
    audit_events: PostgresAuditEventRepository
    connectors: PostgresConnectorRepository
    mcp_child_servers: PostgresMCPChildServerRepository
    mcp_pending_requests: PostgresMCPPendingRepository
    test_runs: PostgresTestRunRepository
    trust_events: PostgresTrustEventRepository
    trust: PostgresTrustRepository
    agent_run_branch_intents: PostgresAgentRunBranchIntentRepository
    agent_run_apply_change_intents: PostgresAgentRunApplyChangeIntentRepository
    agent_run_validation_intents: PostgresAgentRunValidationIntentRepository
    agent_run_pull_request_intents: PostgresAgentRunPullRequestIntentRepository
    recovery_snapshots: PostgresRecoverySnapshotRepository

    @classmethod
    def from_connector(
        cls, connect: Callable[[], Connection]
    ) -> ApplicationRepositories:
        """Build all PostgreSQL adapters over the runtime's shared pool."""
        trust_events = PostgresTrustEventRepository(connect)
        trust = PostgresTrustRepository()
        return cls(
            auth=PostgresAuthRepository(connect),
            auth_rate_limits=PostgresAuthRateLimitRepository(connect),
            email_outbox=PostgresEmailOutboxRepository(connect),
            notification_outbox=PostgresNotificationOutboxRepository(connect),
            approval_decisions=PostgresApprovalDecisionRepository(connect),
            agent_tasks=PostgresAgentTaskRepository(connect),
            projects=PostgresProjectRepository(connect),
            agent_runs=PostgresAgentRunRepository(connect),
            workspace_membership=PostgresWorkspaceMembershipRepository(connect),
            external_agent_auth=PostgresExternalAgentAuthRepository(connect),
            work_orders=PostgresWorkOrderRepository(connect),
            ideas=PostgresIdeaRepository(connect),
            audit_events=PostgresAuditEventRepository(connect),
            connectors=PostgresConnectorRepository(connect),
            mcp_child_servers=PostgresMCPChildServerRepository(connect),
            mcp_pending_requests=PostgresMCPPendingRepository(connect),
            test_runs=PostgresTestRunRepository(connect),
            trust_events=trust_events,
            trust=trust,
            agent_run_branch_intents=PostgresAgentRunBranchIntentRepository(
                connect, trust_repository=trust, trust_event_repository=trust_events
            ),
            agent_run_apply_change_intents=PostgresAgentRunApplyChangeIntentRepository(
                connect, trust_repository=trust, trust_event_repository=trust_events
            ),
            agent_run_validation_intents=PostgresAgentRunValidationIntentRepository(
                connect, trust_repository=trust, trust_event_repository=trust_events
            ),
            agent_run_pull_request_intents=PostgresAgentRunPullRequestIntentRepository(
                connect, trust_repository=trust, trust_event_repository=trust_events
            ),
            recovery_snapshots=PostgresRecoverySnapshotRepository(connect),
        )
