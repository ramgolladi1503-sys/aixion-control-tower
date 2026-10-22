package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalDeliveryStatus
import com.aixion.controltower.core.model.ApprovalHistoryFile
import com.aixion.controltower.core.model.ApprovalHistoryPage
import com.aixion.controltower.core.model.ApprovalHistoryRequestSnapshot
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel

class ApprovalRepository(private val api: ControlTowerApi) {
    suspend fun listApprovals(projectNamesById: Map<String, String> = emptyMap()): List<ApprovalSummary> {
        return api.listApprovals().map { dto ->
            dto.toUiSummary(projectNamesById[dto.project_id] ?: "Project")
        }
    }

    suspend fun getApproval(approvalId: String, projectName: String = "Project"): ApprovalSummary {
        return api.getApproval(approvalId).toUiSummary(projectName)
    }

    suspend fun listApprovalHistory(
        limit: Int = APPROVAL_HISTORY_LIMIT,
        cursor: String? = null
    ): ApprovalHistoryPage {
        require(limit in 1..APPROVAL_HISTORY_LIMIT) { "Approval history limit must be between 1 and $APPROVAL_HISTORY_LIMIT" }
        require(cursor == null || cursor.isNotBlank()) { "Approval history cursor cannot be blank" }
        val response = api.listApprovalHistory(limit, cursor)
        if (response.hasMore && response.nextCursor.isNullOrBlank()) {
            throw IllegalStateException("Approval history page is missing its continuation cursor")
        }
        val events = response.events.map { event ->
            require(event.auditEventId.isNotBlank() && event.approvalId.isNotBlank()) {
                "Approval history event is missing its audit identity"
            }
            val verifiedSnapshot = event.requestSnapshot
                ?.takeIf { event.requestSnapshotVerified }
                ?.let { snapshot ->
                    ApprovalHistoryRequestSnapshot(
                        projectId = snapshot.projectId,
                        title = snapshot.title,
                        summary = snapshot.summary,
                        agentName = snapshot.agentName,
                        targetBranch = snapshot.targetBranch,
                        files = snapshot.files.orEmpty().mapNotNull { file ->
                            file.path?.takeIf(String::isNotBlank)?.let { path ->
                                ApprovalHistoryFile(path = path, changeType = file.changeType.orEmpty())
                            }
                        },
                        testPlan = snapshot.testPlan.orEmpty(),
                        rollbackPlan = snapshot.rollbackPlan,
                        sourceProvider = snapshot.sourceProvider,
                        sourceAgentName = snapshot.sourceAgentName,
                        sourceSessionId = snapshot.sourceSessionId,
                        sourceInvocationId = snapshot.sourceInvocationId,
                        sourceTaskUrl = snapshot.sourceTaskUrl,
                        riskLevel = snapshot.risk?.level
                            ?.let { runCatching { RiskLevel.valueOf(it.uppercase()) }.getOrNull() }
                            ?: RiskLevel.UNKNOWN,
                        riskReasons = snapshot.risk?.reasons.orEmpty(),
                        requiredActions = snapshot.risk?.requiredActions.orEmpty()
                    )
                }
            ApprovalHistoryEvent(
                eventId = event.auditEventId,
                approvalId = event.approvalId,
                decision = event.decision,
                reason = event.reason?.takeIf(String::isNotBlank),
                decidedAt = event.decidedAt,
                reviewerLabel = event.reviewerLabel.toSafeReviewerLabel(),
                deliveryStatus = event.deliveryStatus
                    ?.let { raw -> runCatching { ApprovalDeliveryStatus.valueOf(raw.trim().uppercase()) }.getOrNull() }
                    ?: ApprovalDeliveryStatus.NOT_CONFIRMED,
                requestSnapshotVerified = verifiedSnapshot != null,
                requestSnapshot = verifiedSnapshot
            )
        }
        return ApprovalHistoryPage(
            events = events,
            limit = response.limit.coerceIn(1, APPROVAL_HISTORY_LIMIT),
            hasMore = response.hasMore,
            nextCursor = response.nextCursor?.takeIf(String::isNotBlank)
        )
    }

    suspend fun decide(
        approvalId: String,
        decision: String,
        reason: String,
        projectName: String = "Project"
    ): ApprovalSummary {
        return api.decideApproval(
            approvalId,
            DecisionRequestDto(decision = decision, reason = reason)
        ).toUiSummary(projectName)
    }

    suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto {
        return api.resolveMCPApproval(approvalId)
    }
}

private const val APPROVAL_HISTORY_LIMIT = 50

private fun String?.toSafeReviewerLabel(): String {
    return when (this?.trim()?.lowercase()) {
        "you" -> "You"
        else -> "Workspace reviewer"
    }
}
