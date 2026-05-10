package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.PendingRetryRequestDto
import com.aixion.controltower.core.model.MCPPendingHealthSummary
import com.aixion.controltower.core.model.MCPPendingSummary
import com.aixion.controltower.data.mock.MockData

class MCPPendingRepository(private val api: ControlTowerApi) {
    suspend fun listPendingRequests(): List<MCPPendingSummary> {
        return runCatching {
            api.listMCPPendingRequests().map { it.toUiSummary() }
        }.getOrElse {
            MockData.mcpPendingRequests
        }
    }

    suspend fun getHealth(): MCPPendingHealthSummary {
        return runCatching {
            api.getMCPPendingHealth().toUiSummary()
        }.getOrElse {
            MockData.mcpPendingHealth
        }
    }

    suspend fun recoverPendingRequest(pendingRequestId: String): MCPPendingSummary {
        return api.retryMCPPendingRequest(
            pendingRequestId = pendingRequestId,
            payload = PendingRetryRequestDto(
                reason = "Recover from Android MCP Queue screen",
                reset_attempts = true
            )
        ).toUiSummary()
    }
}
