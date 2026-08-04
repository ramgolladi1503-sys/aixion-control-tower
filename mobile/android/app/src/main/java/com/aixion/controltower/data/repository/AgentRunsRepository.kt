package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.AgentRunsApi
import com.aixion.controltower.core.api.dto.ActionAuthorizationDto
import com.aixion.controltower.core.api.dto.ActionAuthorizationRequestDto
import com.aixion.controltower.core.api.dto.AgentReliabilityScorecardDto
import com.aixion.controltower.core.api.dto.AgentRunControlRequestDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunExecuteRequestDto
import com.aixion.controltower.core.api.dto.AgentRunRetryRequestDto
import com.aixion.controltower.core.api.dto.AgentRunSummaryDto
import com.aixion.controltower.core.api.dto.RelayCommandDto
import com.aixion.controltower.core.api.dto.RelayHostDto
import com.aixion.controltower.core.api.dto.RelaySessionControlDto
import com.aixion.controltower.core.api.dto.RelaySessionCreateDto
import com.aixion.controltower.core.api.dto.RelaySessionDetailDto
import com.aixion.controltower.core.api.dto.RelaySessionDto
import com.aixion.controltower.core.api.dto.RelaySessionMessageDto
import com.aixion.controltower.core.api.dto.RelaySummaryDto
import com.aixion.controltower.core.api.dto.TrustExceptionDto

class AgentRunsRepository(private val api: AgentRunsApi) {
    suspend fun listRuns() = api.listRuns()

    suspend fun getSummary(): AgentRunSummaryDto = api.getSummary()

    suspend fun getRun(runId: String): AgentRunDetailDto = api.getRun(runId)

    suspend fun listTrustExceptions(): List<TrustExceptionDto> = api.listTrustExceptions()

    suspend fun listReliabilityScorecards(): List<AgentReliabilityScorecardDto> =
        api.listReliabilityScorecards()

    suspend fun decideExactAction(
        actionId: String,
        decision: String,
        reason: String
    ): ActionAuthorizationDto = api.decideExactAction(
        actionId,
        ActionAuthorizationRequestDto(decision = decision, reason = reason)
    )

    suspend fun listRelayHosts(): List<RelayHostDto> = api.listRelayHosts()

    suspend fun getRelaySummary(): RelaySummaryDto = api.getRelaySummary()

    suspend fun listRelaySessions(): List<RelaySessionDto> = api.listRelaySessions()

    suspend fun getRelaySession(sessionId: String): RelaySessionDetailDto =
        api.getRelaySession(sessionId)

    suspend fun createRelaySession(request: RelaySessionCreateDto): RelaySessionDetailDto =
        api.createRelaySession(request)

    suspend fun sendRelayMessage(sessionId: String, message: String): RelayCommandDto =
        api.sendRelaySessionMessage(sessionId, RelaySessionMessageDto(message))

    suspend fun pauseRelaySession(sessionId: String, reason: String): RelayCommandDto =
        api.pauseRelaySession(sessionId, RelaySessionControlDto(reason))

    suspend fun resumeRelaySession(sessionId: String, reason: String): RelayCommandDto =
        api.resumeRelaySession(sessionId, RelaySessionControlDto(reason))

    suspend fun cancelRelaySession(sessionId: String, reason: String): RelayCommandDto =
        api.cancelRelaySession(sessionId, RelaySessionControlDto(reason))

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
