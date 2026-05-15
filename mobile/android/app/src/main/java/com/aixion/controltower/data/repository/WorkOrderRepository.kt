package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.WorkOrderSummary

class WorkOrderRepository(private val api: ControlTowerApi) {
    suspend fun listWorkOrders(projectNamesById: Map<String, String> = emptyMap()): List<WorkOrderSummary> {
        return api.listWorkOrders().map { dto ->
            dto.toUiSummary(projectNamesById[dto.project_id] ?: "Project")
        }
    }
}