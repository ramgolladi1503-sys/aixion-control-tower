package com.aixion.controltower.feature.agenttasks

import com.aixion.controltower.core.model.AgentTaskStatus
import com.aixion.controltower.core.model.AgentTaskSummary
import org.junit.Assert.assertEquals
import org.junit.Test

class AgentWorkFilterTest {
    @Test
    fun filtersKeepTerminalHistoryInAllAndSeparateWaitingFromActiveWork() {
        val tasks = listOf(
            task("waiting", AgentTaskStatus.WAITING_FOR_APPROVAL, requiresApproval = true),
            task("active", AgentTaskStatus.EXECUTING),
            task("received", AgentTaskStatus.RECEIVED),
            task("done", AgentTaskStatus.DONE),
            task("failed", AgentTaskStatus.FAILED)
        )

        assertEquals(listOf("waiting", "active", "received", "done", "failed"), ids(tasks.filterBy(AgentWorkFilter.ALL)))
        assertEquals(listOf("waiting"), ids(tasks.filterBy(AgentWorkFilter.WAITING_APPROVAL)))
        assertEquals(listOf("active", "received"), ids(tasks.filterBy(AgentWorkFilter.ACTIVE)))
    }

    private fun ids(tasks: List<AgentTaskSummary>) = tasks.map(AgentTaskSummary::id)

    private fun task(
        id: String,
        status: AgentTaskStatus,
        requiresApproval: Boolean = false
    ) = AgentTaskSummary(
        id = id,
        provider = "test",
        title = id,
        goal = "goal",
        context = "",
        status = status,
        requestedAction = "review",
        repository = null,
        branchPreference = null,
        riskHint = null,
        requiresApproval = requiresApproval,
        approvalRequestId = null,
        sourceUrl = null,
        sourceSessionId = null,
        sourceTaskId = null,
        createdAt = null,
        updatedAt = null
    )
}
