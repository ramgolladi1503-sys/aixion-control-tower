package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.data.mock.MockData

class ApprovalRepository(private val api: ControlTowerApi) {
    suspend fun listApprovals(projectNamesById: Map<String, String> = emptyMap()): List<ApprovalSummary> {
        return runCatching {
            api.listApprovals().map { dto ->
                dto.toUiSummary(projectNamesById[dto.project_id] ?: "Project")
            }
        }.getOrElse {
            MockData.approvals
        }
    }

    suspend fun getApproval(approvalId: String, projectName: String = "Project"): ApprovalSummary {
        return runCatching {
            api.getApproval(approvalId).toUiSummary(projectName)
        }.getOrElse {
            MockData.approvals.firstOrNull { it.id == approvalId } ?: MockData.approvals.first()
        }
    }

    suspend fun decide(approvalId: String, decision: String, reason: String): ApprovalSummary {
        return runCatching {
            api.decideApproval(approvalId, DecisionRequestDto(decision = decision, reason = reason)).toUiSummary()
        }.getOrElse {
            MockData.approvals.firstOrNull { it.id == approvalId } ?: MockData.approvals.first()
        }
    }

    suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto? {
        return runCatching { api.resolveMCPApproval(approvalId) }.getOrNull()
    }
}
