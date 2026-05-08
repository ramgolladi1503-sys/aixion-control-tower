package com.aixion.controltower.core.model

enum class RiskLevel {
    LOW,
    MEDIUM,
    HIGH,
    CRITICAL,
    BLOCKED
}

enum class ApprovalStatus {
    PENDING_REVIEW,
    APPROVED,
    REJECTED,
    REVISION_REQUESTED,
    BLOCKED,
    APPLIED,
    TESTS_RUNNING,
    TESTS_PASSED,
    TESTS_FAILED,
    READY_FOR_PR
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

val ApprovalSummary.requiresHumanAction: Boolean
    get() = when (status) {
        ApprovalStatus.PENDING_REVIEW,
        ApprovalStatus.BLOCKED,
        ApprovalStatus.REVISION_REQUESTED,
        ApprovalStatus.TESTS_FAILED,
        ApprovalStatus.READY_FOR_PR -> true
        ApprovalStatus.APPROVED,
        ApprovalStatus.APPLIED,
        ApprovalStatus.TESTS_RUNNING,
        ApprovalStatus.TESTS_PASSED,
        ApprovalStatus.REJECTED -> false
    }

val ApprovalSummary.isAwaitingGitHubExecution: Boolean
    get() = status == ApprovalStatus.APPROVED

val ApprovalSummary.isReadyForPullRequestReview: Boolean
    get() = status == ApprovalStatus.READY_FOR_PR

val ApprovalSummary.requiresHumanAction: Boolean
    get() = when (status) {
        ApprovalStatus.PENDING_REVIEW,
        ApprovalStatus.BLOCKED,
        ApprovalStatus.REVISION_REQUESTED,
        ApprovalStatus.TESTS_FAILED,
        ApprovalStatus.READY_FOR_PR -> true
        ApprovalStatus.APPROVED,
        ApprovalStatus.APPLIED,
        ApprovalStatus.TESTS_RUNNING,
        ApprovalStatus.TESTS_PASSED,
        ApprovalStatus.REJECTED -> false
    }

val ApprovalSummary.isAwaitingGitHubExecution: Boolean
    get() = status == ApprovalStatus.APPROVED

val ApprovalSummary.isReadyForPullRequestReview: Boolean
    get() = status == ApprovalStatus.READY_FOR_PR

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
