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
    TESTS_PASSED,
    TESTS_FAILED
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
    val rollbackPlan: String
)

data class WorkOrderSummary(
    val id: String,
    val projectName: String,
    val goal: String,
    val risk: RiskLevel,
    val tasks: List<String>,
    val requiredTests: List<String>
)

data class AuditEventSummary(
    val id: String,
    val eventType: String,
    val actor: String,
    val details: String,
    val timestamp: String
)
