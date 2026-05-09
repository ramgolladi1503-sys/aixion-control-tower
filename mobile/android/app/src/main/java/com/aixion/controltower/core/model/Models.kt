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
