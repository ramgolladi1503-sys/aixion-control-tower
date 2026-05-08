package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.WorkOrderSummary
import com.aixion.controltower.data.mock.MockData

class WorkOrderRepository(private val api: ControlTowerApi) {
    suspend fun listWorkOrders(projectNamesById: Map<String, String> = emptyMap()): List<WorkOrderSummary> {
        return runCatching {
            api.listWorkOrders().map { dto ->
                dto.toUiSummary(projectNamesById[dto.project_id] ?: "Project")
            }
        }.getOrElse {
            MockData.workOrders
        }
    }
}
