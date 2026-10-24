from __future__ import annotations

from .agent_run_repository import AgentRunRepository, SQLiteAgentRunRepository
from .agent_task_repository import AgentTaskRepository, SQLiteAgentTaskRepository
from .approval_repository import ApprovalDecisionRepository, SQLiteApprovalDecisionRepository
from .project_repository import ProjectRepository, SQLiteProjectRepository
from .store import store
from .workspace_membership_repository import (
    SQLiteWorkspaceMembershipRepository,
    WorkspaceMembershipRepository,
)

# One adapter instance is shared by API decision writes and host decision reads.
# Replace this binding when a production database adapter is introduced; routes
# must not select their own persistence implementation.
_sqlite_approval_repository = SQLiteApprovalDecisionRepository(store._connect)
approval_decision_repository: ApprovalDecisionRepository = _sqlite_approval_repository
agent_task_repository: AgentTaskRepository = SQLiteAgentTaskRepository(
    _sqlite_approval_repository._connect
)
workspace_membership_repository: WorkspaceMembershipRepository = (
    SQLiteWorkspaceMembershipRepository(_sqlite_approval_repository._connect)
)
project_repository: ProjectRepository = SQLiteProjectRepository(
    _sqlite_approval_repository._connect
)
agent_run_repository: AgentRunRepository = SQLiteAgentRunRepository(
    _sqlite_approval_repository._connect
)
