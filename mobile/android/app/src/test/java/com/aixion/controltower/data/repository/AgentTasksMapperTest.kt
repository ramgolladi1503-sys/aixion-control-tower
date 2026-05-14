package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.AgentTaskDto
import com.aixion.controltower.core.api.dto.AgentTaskEventDto
import com.aixion.controltower.core.model.AgentTaskStatus
import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentTasksMapperTest {
    @Test
    fun agentTaskDtoMapsToUiSummary() {
        val summary = AgentTaskDto(
            id = "agent_task_123",
            provider = "CHATGPT",
            project_id = "project_1",
            title = "Build Android Agent Tasks screen",
            goal = "Show connected-agent work on mobile.",
            context = "Need timeline and linked approval visibility.",
            source_session_id = "session_123",
            source_task_id = "task_external_123",
            requested_action = "CREATE_APPROVAL",
            repository = "ramgolladi1503-sys/aixion-control-tower",
            branch_preference = "feature/pr81-android-agent-tasks-screen",
            risk_hint = "HIGH",
            requires_approval = true,
            status = "WAITING_FOR_APPROVAL",
            approval_request_id = "approval_123",
            created_at = "2026-05-14T20:00:00Z",
            updated_at = "2026-05-14T20:05:00Z"
        ).toUiSummary()

        assertEquals("agent_task_123", summary.id)
        assertEquals("CHATGPT", summary.provider)
        assertEquals("Build Android Agent Tasks screen", summary.title)
        assertEquals(AgentTaskStatus.WAITING_FOR_APPROVAL, summary.status)
        assertEquals("CREATE_APPROVAL", summary.requestedAction)
        assertEquals("ramgolladi1503-sys/aixion-control-tower", summary.repository)
        assertEquals(RiskLevel.HIGH, summary.riskHint)
        assertTrue(summary.requiresApproval)
        assertTrue(summary.hasLinkedApproval)
        assertTrue(summary.needsHumanReview)
    }

    @Test
    fun unknownTaskStatusFallsBackToReceived() {
        val summary = AgentTaskDto(
            id = "agent_task_unknown",
            title = "Unknown state task",
            goal = "Should not crash mapper.",
            status = "NOT_A_REAL_STATUS"
        ).toUiSummary()

        assertEquals(AgentTaskStatus.RECEIVED, summary.status)
    }

    @Test
    fun agentTaskEventDtoMapsToUiSummary() {
        val event = AgentTaskEventDto(
            id = "agent_task_event_123",
            task_id = "agent_task_123",
            event_type = "APPROVAL_CREATED",
            message = "Approval request created for agent task.",
            status = "WAITING_FOR_APPROVAL",
            actor = "operator",
            created_at = "2026-05-14T20:06:00Z"
        ).toUiSummary()

        assertEquals("agent_task_event_123", event.id)
        assertEquals("agent_task_123", event.taskId)
        assertEquals("APPROVAL_CREATED", event.eventType)
        assertEquals("Approval request created for agent task.", event.message)
        assertEquals(AgentTaskStatus.WAITING_FOR_APPROVAL, event.status)
        assertEquals("operator", event.actor)
    }
}
