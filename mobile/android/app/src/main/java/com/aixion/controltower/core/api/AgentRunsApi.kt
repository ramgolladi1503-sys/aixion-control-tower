package com.aixion.controltower.core.api

import com.aixion.controltower.core.api.dto.ActionAuthorizationDto
import com.aixion.controltower.core.api.dto.ActionAuthorizationRequestDto
import com.aixion.controltower.core.api.dto.AgentReliabilityScorecardDto
import com.aixion.controltower.core.api.dto.AgentRunControlRequestDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunDto
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
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface AgentRunsApi {
    @GET("agent/runs")
    suspend fun listRuns(
        @Query("status") status: String? = null,
        @Query("task_id") taskId: String? = null,
        @Query("limit") limit: Int = 100
    ): List<AgentRunDto>

    @GET("agent/runs/summary")
    suspend fun getSummary(): AgentRunSummaryDto

    @GET("agent/runs/{runId}")
    suspend fun getRun(@Path("runId") runId: String): AgentRunDetailDto

    @GET("trust/exceptions")
    suspend fun listTrustExceptions(): List<TrustExceptionDto>

    @GET("trust/scorecards")
    suspend fun listReliabilityScorecards(): List<AgentReliabilityScorecardDto>

    @POST("trust/gateway/actions/{actionId}/decision")
    suspend fun decideExactAction(
        @Path("actionId") actionId: String,
        @Body request: ActionAuthorizationRequestDto
    ): ActionAuthorizationDto

    @GET("connectors/relay-hosts")
    suspend fun listRelayHosts(): List<RelayHostDto>

    @GET("connectors/relay-hosts/summary")
    suspend fun getRelaySummary(): RelaySummaryDto

    @GET("connectors/relay-sessions")
    suspend fun listRelaySessions(
        @Query("status") status: String? = null,
        @Query("relay_id") relayId: String? = null,
        @Query("provider") provider: String? = null,
        @Query("limit") limit: Int = 100
    ): List<RelaySessionDto>

    @GET("connectors/relay-sessions/{sessionId}")
    suspend fun getRelaySession(
        @Path("sessionId") sessionId: String
    ): RelaySessionDetailDto

    @POST("connectors/relay-sessions")
    suspend fun createRelaySession(
        @Body request: RelaySessionCreateDto
    ): RelaySessionDetailDto

    @POST("connectors/relay-sessions/{sessionId}/messages")
    suspend fun sendRelaySessionMessage(
        @Path("sessionId") sessionId: String,
        @Body request: RelaySessionMessageDto
    ): RelayCommandDto

    @POST("connectors/relay-sessions/{sessionId}/pause")
    suspend fun pauseRelaySession(
        @Path("sessionId") sessionId: String,
        @Body request: RelaySessionControlDto
    ): RelayCommandDto

    @POST("connectors/relay-sessions/{sessionId}/resume")
    suspend fun resumeRelaySession(
        @Path("sessionId") sessionId: String,
        @Body request: RelaySessionControlDto
    ): RelayCommandDto

    @POST("connectors/relay-sessions/{sessionId}/cancel")
    suspend fun cancelRelaySession(
        @Path("sessionId") sessionId: String,
        @Body request: RelaySessionControlDto
    ): RelayCommandDto

    @POST("agent/runs/{runId}/pause")
    suspend fun pauseRun(
        @Path("runId") runId: String,
        @Body request: AgentRunControlRequestDto
    ): AgentRunDetailDto

    @POST("agent/runs/{runId}/resume")
    suspend fun resumeRun(
        @Path("runId") runId: String,
        @Body request: AgentRunControlRequestDto
    ): AgentRunDetailDto

    @POST("agent/runs/{runId}/cancel")
    suspend fun cancelRun(
        @Path("runId") runId: String,
        @Body request: AgentRunControlRequestDto
    ): AgentRunDetailDto

    @POST("agent/runs/{runId}/retry")
    suspend fun retryRun(
        @Path("runId") runId: String,
        @Body request: AgentRunRetryRequestDto
    ): AgentRunDetailDto

    @POST("agent/runs/{runId}/execute-next")
    suspend fun executeNext(
        @Path("runId") runId: String,
        @Body request: AgentRunExecuteRequestDto
    ): AgentRunDetailDto
}
