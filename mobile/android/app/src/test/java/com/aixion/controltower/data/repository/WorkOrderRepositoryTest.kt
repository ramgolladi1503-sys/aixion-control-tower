package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.AuditEventDto
import com.aixion.controltower.core.api.dto.WorkOrderDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class WorkOrderRepositoryTest {
    @Test
    fun listWorkOrdersThrowsWhenBackendListFails() = runTest {
        val repository = WorkOrderRepository(FailingWorkOrderApi())

        try {
            repository.listWorkOrders()
        } catch (error: IllegalStateException) {
            return@runTest
        }

        fail("Expected IllegalStateException instead of mock work-order fallback")
    }

    @Test
    fun listWorkOrdersMapsManualSource() = runTest {
        val repository = WorkOrderRepository(
            SourceWorkOrderApi(
                workOrders = listOf(workOrderDto("work_manual")),
                auditEvents = listOf(
                    sourceEvent(
                        entityId = "work_manual",
                        eventType = "work_order.created",
                        details = mapOf(
                            "source_type" to "MANUAL",
                            "source_provider" to "MANUAL",
                            "created_by_user_id" to "dev_user",
                            "verified_source" to false
                        )
                    )
                )
            )
        )

        val result = repository.listWorkOrders().single()

        assertEquals("MANUAL", result.sourceType)
        assertEquals("MANUAL", result.sourceProvider)
        assertEquals("dev_user", result.createdByUserId)
        assertFalse(result.verifiedSource)
        assertEquals("Manual User Source", result.sourceBadgeLabel)
    }

    @Test
    fun listWorkOrdersMapsVerifiedAgentSource() = runTest {
        val repository = WorkOrderRepository(
            SourceWorkOrderApi(
                workOrders = listOf(workOrderDto("work_agent")),
                auditEvents = listOf(
                    sourceEvent(
                        entityId = "work_agent",
                        eventType = "work_order.created_by_agent",
                        details = mapOf(
                            "source_type" to "AGENT_TASK",
                            "source_provider" to "CHATGPT",
                            "source_agent_id" to "agent_demo",
                            "source_agent_name" to "Scoped GPT",
                            "source_task_id" to "task_demo",
                            "source_session_id" to "session_demo",
                            "verified_source" to true
                        )
                    )
                )
            )
        )

        val result = repository.listWorkOrders().single()

        assertEquals("AGENT_TASK", result.sourceType)
        assertEquals("CHATGPT", result.sourceProvider)
        assertEquals("agent_demo", result.sourceAgentId)
        assertEquals("Scoped GPT", result.sourceAgentName)
        assertEquals("task_demo", result.sourceTaskId)
        assertEquals("session_demo", result.sourceSessionId)
        assertTrue(result.verifiedSource)
        assertEquals("Verified Agent Source", result.sourceBadgeLabel)
        assertEquals("Scoped GPT", result.sourceLabel)
    }

    @Test
    fun listWorkOrdersFallsBackWhenSourceEventsFail() = runTest {
        val repository = WorkOrderRepository(
            SourceWorkOrderApi(
                workOrders = listOf(workOrderDto("work_without_source")),
                shouldFailAuditEvents = true
            )
        )

        val result = repository.listWorkOrders().single()

        assertEquals("MANUAL", result.sourceType)
        assertEquals("MANUAL", result.sourceProvider)
        assertFalse(result.verifiedSource)
    }
}

private class FailingWorkOrderApi : BaseFakeControlTowerApi() {
    override suspend fun listWorkOrders(): List<WorkOrderDto> {
        throw IllegalStateException("backend work-order list failed")
    }
}

private class SourceWorkOrderApi(
    private val workOrders: List<WorkOrderDto>,
    private val auditEvents: List<AuditEventDto> = emptyList(),
    private val shouldFailAuditEvents: Boolean = false
) : BaseFakeControlTowerApi() {
    override suspend fun listWorkOrders(): List<WorkOrderDto> = workOrders

    override suspend fun listAuditEvents(): List<AuditEventDto> {
        if (shouldFailAuditEvents) throw IllegalStateException("source events unavailable")
        return auditEvents
    }
}

private fun workOrderDto(id: String): WorkOrderDto {
    return WorkOrderDto(
        id = id,
        project_id = "project_demo",
        goal = "Trace WorkOrder source",
        context = "",
        tasks = listOf("Map source metadata"),
        required_tests = listOf("unit tests"),
        risk_level = "MEDIUM"
    )
}

private fun sourceEvent(
    entityId: String,
    eventType: String,
    details: Map<String, Any?>
): AuditEventDto {
    return AuditEventDto(
        id = "audit_$entityId",
        event_type = eventType,
        actor = "test",
        entity_id = entityId,
        details = details,
        created_at = "2026-05-16T06:30:00Z"
    )
}
