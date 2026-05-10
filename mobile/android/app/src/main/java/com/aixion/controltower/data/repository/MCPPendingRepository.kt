package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.PendingRetryRequestDto
import com.aixion.controltower.core.model.MCPPendingHealthSummary
import com.aixion.controltower.core.model.MCPPendingSummary

class MCPPendingRepository(private val api: ControlTowerApi) {
    suspend fun listPendingRequests(): List<MCPPendingSummary> {
        return api.listMCPPendingRequests().map { it.toUiSummary() }
    }

    suspend fun getHealth(): MCPPendingHealthSummary {
        return api.getMCPPendingHealth().toUiSummary()
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
