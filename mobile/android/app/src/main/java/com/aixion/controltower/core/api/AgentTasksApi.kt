package com.aixion.controltower.core.api

import com.aixion.controltower.core.api.dto.AgentTaskDto
import com.aixion.controltower.core.api.dto.AgentTaskEventDto
import retrofit2.http.GET
import retrofit2.http.Path
import retrofit2.http.Query

interface AgentTasksApi {
    @GET("agent/tasks")
    suspend fun listAgentTasks(
        @Query("provider") provider: String? = null,
        @Query("status") status: String? = null,
        @Query("project_id") projectId: String? = null,
        @Query("limit") limit: Int = 100
    ): List<AgentTaskDto>

    @GET("agent/tasks/{taskId}")
    suspend fun getAgentTask(@Path("taskId") taskId: String): AgentTaskDto

    @GET("agent/tasks/{taskId}/events")
    suspend fun listAgentTaskEvents(@Path("taskId") taskId: String): List<AgentTaskEventDto>
}
