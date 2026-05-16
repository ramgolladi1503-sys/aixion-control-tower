package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.model.WorkOrderSummary

class WorkOrderRepository(private val api: ControlTowerApi) {
    suspend fun listWorkOrders(projectNamesById: Map<String, String> = emptyMap()): List<WorkOrderSummary> {
        val sourceMetadataByWorkOrderId = runCatching {
            api.listAuditEvents()
                .filter { event -> event.event_type == "work_order.created" || event.event_type == "work_order.created_by_agent" }
                .associate { event -> event.entity_id to event.details.toWorkOrderSourceMetadata() }
        }.getOrDefault(emptyMap())

        return api.listWorkOrders().map { dto ->
            dto.toUiSummary(
                projectName = projectNamesById[dto.project_id] ?: "Project",
                sourceMetadata = sourceMetadataByWorkOrderId[dto.id] ?: WorkOrderSourceMetadata()
            )
        }
    }
}
