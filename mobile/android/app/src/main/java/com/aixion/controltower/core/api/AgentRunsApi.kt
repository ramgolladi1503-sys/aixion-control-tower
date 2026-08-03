package com.aixion.controltower.core.api

import com.aixion.controltower.core.api.dto.AgentRunControlRequestDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunDto
import com.aixion.controltower.core.api.dto.AgentRunExecuteRequestDto
import com.aixion.controltower.core.api.dto.AgentRunRetryRequestDto
import com.aixion.controltower.core.api.dto.AgentRunSummaryDto
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
