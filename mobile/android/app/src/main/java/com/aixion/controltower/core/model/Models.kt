package com.aixion.controltower.core.model

enum class RiskLevel {
    LOW,
    MEDIUM,
    HIGH,
    CRITICAL,
    BLOCKED
}

enum class ApprovalStatus {
    REQUESTED,
    APPROVED,
    DENIED,
    EXPIRED,
    EXECUTING,
    READY_FOR_PR,
    FAILED,
    MERGED,
    CANCELLED,

    PENDING_REVIEW,
    REJECTED,
    REVISION_REQUESTED,
    BLOCKED,
    APPLIED,
    TESTS_RUNNING,
    TESTS_PASSED,
    TESTS_FAILED
}

enum class ApprovalDashboardBucket {
    NEEDS_APPROVAL,
    APPROVED_WAITING,
    EXECUTING,
    READY_FOR_PR_REVIEW,
    FAILED,
    COMPLETED,
    HISTORY
}

enum class MCPPendingStatus {
    WAITING_FOR_APPROVAL,
    FORWARDING,
    FORWARDED,
    BLOCKED_BY_DECISION,
    ORPHANED,
    DEAD_LETTER
}

enum class AgentTaskStatus {
    RECEIVED,
    PLANNING,
    WAITING_FOR_APPROVAL,
    APPROVED,
    DENIED,
    EXECUTING,
    TESTING,
    READY_FOR_PR,
    FAILED,
    CANCELLED,
    DONE
}

data class ProjectSummary(
    val id: String,
    val name: String,
    val description: String,
    val mode: String,
    val pendingApprovals: Int,
    val blockedRequests: Int,
    val healthScore: Int
)

data class FileChangeSummary(
    val path: String,
    val changeType: String,
    val diff: String,
    val risk: RiskLevel
)

data class ApprovalSummary(
    val id: String,
    val projectName: String,
    val title: String,
    val summary: String,
    val agentName: String,
    val targetBranch: String,
    val status: ApprovalStatus,
    val risk: RiskLevel,
    val files: List<FileChangeSummary>,
    val requiredActions: List<String>,
    val testPlan: List<String>,
    val rollbackPlan: String,
    val sourceProvider: String = "MANUAL",
    val sourceAgentId: String? = null,
    val sourceAgentName: String? = null,
    val sourceSessionId: String? = null,
    val sourceTaskUrl: String? = null,
    val createdByUserId: String? = null,
    val verifiedSource: Boolean = false
) {
    val sourceLabel: String
        get() = sourceAgentName?.takeIf { it.isNotBlank() } ?: sourceProvider

    val isMCPToolApproval: Boolean
        get() = files.any { file ->
            file.path.startsWith("mcp://") || file.changeType == "mcp_tool_call"
        }
}

val ApprovalStatus.canonical: ApprovalStatus
    get() = when (this) {
        ApprovalStatus.PENDING_REVIEW -> ApprovalStatus.REQUESTED
        ApprovalStatus.REJECTED -> ApprovalStatus.DENIED
        ApprovalStatus.TESTS_RUNNING,
        ApprovalStatus.APPLIED -> ApprovalStatus.EXECUTING
        ApprovalStatus.TESTS_FAILED -> ApprovalStatus.FAILED
        ApprovalStatus.TESTS_PASSED -> ApprovalStatus.MERGED
        else -> this
    }

val ApprovalSummary.dashboardBucket: ApprovalDashboardBucket
    get() = when (status.canonical) {
        ApprovalStatus.REQUESTED,
        ApprovalStatus.BLOCKED,
        ApprovalStatus.REVISION_REQUESTED -> ApprovalDashboardBucket.NEEDS_APPROVAL
        ApprovalStatus.APPROVED -> ApprovalDashboardBucket.APPROVED_WAITING
        ApprovalStatus.EXECUTING -> ApprovalDashboardBucket.EXECUTING
        ApprovalStatus.READY_FOR_PR -> ApprovalDashboardBucket.READY_FOR_PR_REVIEW
        ApprovalStatus.FAILED -> ApprovalDashboardBucket.FAILED
        ApprovalStatus.MERGED -> ApprovalDashboardBucket.COMPLETED
        ApprovalStatus.DENIED,
        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED -> ApprovalDashboardBucket.HISTORY
        ApprovalStatus.PENDING_REVIEW,
        ApprovalStatus.REJECTED,
        ApprovalStatus.APPLIED,
        ApprovalStatus.TESTS_RUNNING,
        ApprovalStatus.TESTS_PASSED,
        ApprovalStatus.TESTS_FAILED -> error("Legacy status should have been canonicalized before bucketing")
    }

val ApprovalSummary.requiresHumanAction: Boolean
    get() = dashboardBucket == ApprovalDashboardBucket.NEEDS_APPROVAL

val ApprovalSummary.isAwaitingGitHubExecution: Boolean
    get() = dashboardBucket == ApprovalDashboardBucket.APPROVED_WAITING

val ApprovalSummary.isReadyForPullRequestReview: Boolean
    get() = dashboardBucket == ApprovalDashboardBucket.READY_FOR_PR_REVIEW

data class WorkOrderSummary(
    val id: String,
    val projectName: String,
    val goal: String,
    val risk: RiskLevel,
    val tasks: List<String>,
    val requiredTests: List<String>
)

data class TestRunSummary(
    val id: String,
    val approvalRequestId: String,
    val command: String,
    val status: String,
    val outputSummary: String
)

data class AuditEventSummary(
    val id: String,
    val eventType: String,
    val actor: String,
    val details: String,
    val timestamp: String
)

data class RuntimeReadinessSummary(
    val status: String,
    val generatedAt: String?,
    val profile: String,
    val authEnabled: Boolean,
    val dbReachable: Boolean,
    val migrationsApplied: Boolean,
    val expectedMigrationIds: List<String>,
    val appliedMigrationIds: List<String>,
    val recoverySnapshotAvailable: Boolean,
    val recoveryFormatVersion: String,
    val githubTokenConfigured: Boolean,
    val fcmServerKeyConfigured: Boolean,
    val errors: List<String>,
    val warnings: List<String>
) {
    val isReady: Boolean
        get() = status.equals("ready", ignoreCase = true) && errors.isEmpty()

    val readinessLabel: String
        get() = if (isReady) "Ready" else "Needs review"

    val missingMigrationIds: List<String>
        get() = expectedMigrationIds.filterNot { expected -> expected in appliedMigrationIds }

    val hasSecretWarnings: Boolean
        get() = !githubTokenConfigured || !fcmServerKeyConfigured
}

data class AgentTaskSummary(
    val id: String,
    val provider: String,
    val title: String,
    val goal: String,
    val context: String,
    val status: AgentTaskStatus,
    val requestedAction: String,
    val repository: String?,
    val branchPreference: String?,
    val riskHint: RiskLevel?,
    val requiresApproval: Boolean,
    val approvalRequestId: String?,
    val sourceUrl: String?,
    val sourceSessionId: String?,
    val sourceTaskId: String?,
    val createdAt: String?,
    val updatedAt: String?
) {
    val sourceLabel: String
        get() = provider.ifBlank { "MANUAL" }

    val needsHumanReview: Boolean
        get() = status == AgentTaskStatus.WAITING_FOR_APPROVAL || status == AgentTaskStatus.RECEIVED || status == AgentTaskStatus.PLANNING

    val hasLinkedApproval: Boolean
        get() = !approvalRequestId.isNullOrBlank()
}

data class AgentTaskEventSummary(
    val id: String,
    val taskId: String,
    val eventType: String,
    val message: String,
    val status: AgentTaskStatus?,
    val actor: String,
    val createdAt: String?
)

data class MCPPendingSummary(
    val id: String,
    val projectId: String,
    val approvalRequestId: String,
    val serverName: String,
    val toolName: String,
    val requestedBy: String,
    val status: MCPPendingStatus,
    val attempts: Int,
    val maxAttempts: Int,
    val leaseOwner: String?,
    val leaseExpiresAt: String?,
    val lastError: String?,
    val createdAt: String?,
    val updatedAt: String?
) {
    val isRetryable: Boolean
        get() = status == MCPPendingStatus.WAITING_FOR_APPROVAL || status == MCPPendingStatus.DEAD_LETTER

    val needsOperatorAttention: Boolean
        get() = status == MCPPendingStatus.DEAD_LETTER || lastError != null
}

data class MCPPendingHealthSummary(
    val total: Int,
    val byStatus: Map<String, Int>,
    val waitingForApproval: Int,
    val forwarding: Int,
    val activeLeases: Int,
    val expiredLeases: Int,
    val retryable: Int,
    val deadLetter: Int,
    val terminal: Int,
    val attentionRequired: Int,
    val oldestWaitingCreatedAt: String?,
    val oldestDeadLetterCreatedAt: String?
) {
    val isHealthy: Boolean
        get() = attentionRequired == 0 && expiredLeases == 0 && deadLetter == 0
}
