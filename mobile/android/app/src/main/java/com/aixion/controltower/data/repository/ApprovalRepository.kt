package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.model.ApprovalSummary

class ApprovalRepository(private val api: ControlTowerApi) {
    suspend fun listApprovals(projectNamesById: Map<String, String> = emptyMap()): List<ApprovalSummary> {
        return api.listApprovals().map { dto ->
            dto.toUiSummary(projectNamesById[dto.project_id] ?: "Project")
        }
    }

    suspend fun getApproval(approvalId: String, projectName: String = "Project"): ApprovalSummary {
        return api.getApproval(approvalId).toUiSummary(projectName)
    }

    suspend fun decide(approvalId: String, decision: String, reason: String): ApprovalSummary {
        return api.decideApproval(
            approvalId,
            DecisionRequestDto(decision = decision, reason = reason)
        ).toUiSummary()
    }

    suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto {
        return api.resolveMCPApproval(approvalId)
    }
}