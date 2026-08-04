package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.AgentRunsApi
import com.aixion.controltower.core.api.dto.AgentRunControlRequestDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunExecuteRequestDto
import com.aixion.controltower.core.api.dto.AgentRunRetryRequestDto
import com.aixion.controltower.core.api.dto.AgentRunSummaryDto

class AgentRunsRepository(private val api: AgentRunsApi) {
    suspend fun listRuns() = api.listRuns()

    suspend fun getSummary(): AgentRunSummaryDto = api.getSummary()

    suspend fun getRun(runId: String): AgentRunDetailDto = api.getRun(runId)

    suspend fun pause(runId: String, reason: String): AgentRunDetailDto =
        api.pauseRun(runId, AgentRunControlRequestDto(reason))

    suspend fun resume(runId: String, reason: String): AgentRunDetailDto =
        api.resumeRun(runId, AgentRunControlRequestDto(reason))

    suspend fun cancel(runId: String, reason: String): AgentRunDetailDto =
        api.cancelRun(runId, AgentRunControlRequestDto(reason))

    suspend fun retry(runId: String, stepId: String?, reason: String): AgentRunDetailDto =
        api.retryRun(runId, AgentRunRetryRequestDto(reason = reason, stepId = stepId))

    suspend fun executeNext(runId: String): AgentRunDetailDto =
        api.executeNext(runId, AgentRunExecuteRequestDto())
}
