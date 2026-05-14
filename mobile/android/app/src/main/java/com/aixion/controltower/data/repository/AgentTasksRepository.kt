package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.AgentTaskEventSummary
import com.aixion.controltower.core.model.AgentTaskSummary

class AgentTasksRepository(private val api: ControlTowerApi) {
    suspend fun listAgentTasks(): List<AgentTaskSummary> {
        return try {
            api.listAgentTasks().map { it.toUiSummary() }
        } catch (error: Exception) {
            throw IllegalStateException("Unable to load agent tasks", error)
        }
    }

    suspend fun listAgentTaskEvents(taskId: String): List<AgentTaskEventSummary> {
        return try {
            api.listAgentTaskEvents(taskId).map { it.toUiSummary() }
        } catch (error: Exception) {
            throw IllegalStateException("Unable to load agent task timeline", error)
        }
    }
}
