package com.aixion.controltower.feature.approvals

import android.app.Application
import android.content.Context
import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryEventDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryResponseDto
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.RiskAssessmentDto
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.data.repository.ApprovalRepository
import com.aixion.controltower.data.repository.BaseFakeControlTowerApi
import java.io.IOException
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ApprovalStaleDecisionViewModelTest {
    @Test
    fun successfulInboxSyncTimeIsRetainedAfterLaterRefreshFailure() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val syncedAt = Instant.parse("2026-10-22T05:30:00Z")
            val api = StaleApprovalApi(terminalOnRefresh = false).apply {
                approvalRows = listOf(approvalDto(APPROVAL_ID, "REQUESTED"))
            }
            val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
            val viewModel = ApprovalsViewModel(
                application,
                ApprovalRepository(api),
                Clock.fixed(syncedAt, ZoneOffset.UTC)
            )
            viewModel.refresh()
            runCurrent()

            assertEquals(syncedAt, viewModel.state.value.lastSuccessfulSyncAt)
            assertEquals(listOf(APPROVAL_ID), viewModel.state.value.approvals.map { it.id })

            api.failApprovalRefresh = true
            viewModel.refresh()
            runCurrent()

            assertEquals(syncedAt, viewModel.state.value.lastSuccessfulSyncAt)
            assertTrue(viewModel.state.value.hasError)
            assertEquals(listOf(APPROVAL_ID), viewModel.state.value.approvals.map { it.id })
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun alreadyExpiredMatchingApprovalDoesNotSendDecision() = runTest {
        assertExpiredRequestBlocksDecision(
            now = Instant.parse("2026-10-22T12:00:00Z"),
            expiresAt = "2026-10-22T11:59:59Z"
        )
    }

    @Test
    fun approvalAtExactExpiryBoundaryDoesNotSendDecision() = runTest {
        assertExpiredRequestBlocksDecision(
            now = Instant.parse("2026-10-22T12:00:00Z"),
            expiresAt = "2026-10-22T12:00:00Z"
        )
    }

    private suspend fun TestScope.assertExpiredRequestBlocksDecision(now: Instant, expiresAt: String) {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = StaleApprovalApi(terminalOnRefresh = false, expiresAt = expiresAt)
            val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
            val clock = Clock.fixed(now, ZoneOffset.UTC)
            val viewModel = ApprovalsViewModel(application, ApprovalRepository(api), clock)
            viewModel.refresh()
            runCurrent()
            viewModel.selectApproval(staleApprovalSummary(expiresAt))

            viewModel.decide("approve", "reviewed")
            runCurrent()

            assertEquals("Expired approvals must not receive a decision POST.", 0, api.decisionCalls)
            assertTrue(api.decisionApprovalIds.isEmpty())
            assertEquals(listOf(APPROVAL_ID), api.statusApprovalIds)
            assertEquals(ApprovalStatus.REQUESTED, viewModel.state.value.selectedApproval?.status)
            assertFalse(viewModel.state.value.decisionInFlight)
            assertFalse(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(APPROVAL_ID, viewModel.state.value.lastActionApprovalId)
            assertTrue(viewModel.state.value.lastActionMessage.orEmpty().contains("expired"))
            assertTrue(viewModel.state.value.lastActionMessage.orEmpty().contains("No decision was sent."))
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun staleApproveReconcilesToServerDeniedStateWithoutRetry() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = StaleApprovalApi()
            val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
            val viewModel = ApprovalsViewModel(application, ApprovalRepository(api))
            viewModel.refresh()
            runCurrent()
            viewModel.selectApproval(staleApprovalSummary())

            viewModel.decide("approve", "reviewed after inbox refresh")
            runCurrent()

            assertEquals("A stale approval must not receive a decision POST.", 0, api.decisionCalls)
            assertTrue(api.decisionApprovalIds.isEmpty())
            assertEquals(listOf(APPROVAL_ID), api.statusApprovalIds)
            assertEquals(ApprovalStatus.DENIED, viewModel.state.value.selectedApproval?.status)
            assertTrue(viewModel.state.value.approvals.none {
                it.id == APPROVAL_ID && it.status == ApprovalStatus.APPROVED
            })
            assertFalse(viewModel.state.value.decisionInFlight)
            assertFalse(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(APPROVAL_ID, viewModel.state.value.lastActionApprovalId)
            assertTrue(
                viewModel.state.value.lastActionMessage.orEmpty()
                    .contains("No decision was sent.")
            )

            viewModel.decide("approve", "retry must remain blocked for the terminal request")
            runCurrent()
            assertEquals("A stale approval must not receive any decision POST.", 0, api.decisionCalls)
            assertTrue(api.decisionApprovalIds.isEmpty())
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun approvalHistoryLoadsOlderPageAppendingInOrderAndDeduplicatingOverlap() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = StaleApprovalApi()
            api.historyPages[null] = ApprovalHistoryResponseDto(
                events = listOf(historyDto("event-1")),
                limit = 2,
                hasMore = true,
                nextCursor = "cursor-1"
            )
            api.historyPages["cursor-1"] = ApprovalHistoryResponseDto(
                events = listOf(historyDto("event-1"), historyDto("event-2")),
                limit = 2,
                hasMore = true,
                nextCursor = "cursor-2"
            )
            api.historyPages["cursor-2"] = ApprovalHistoryResponseDto(
                events = listOf(historyDto("event-3")),
                limit = 2,
                hasMore = false
            )
            val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
            val viewModel = ApprovalsViewModel(application, ApprovalRepository(api))
            runCurrent()

            viewModel.loadApprovalHistory()
            runCurrent()
            assertEquals(listOf("event-1"), viewModel.state.value.approvalHistory.map { it.eventId })
            assertEquals("cursor-1", viewModel.state.value.historyNextCursor)

            viewModel.loadOlderApprovalHistory()
            viewModel.loadOlderApprovalHistory()
            runCurrent()
            assertEquals(listOf("event-1", "event-2"), viewModel.state.value.approvalHistory.map { it.eventId })
            assertEquals("cursor-2", viewModel.state.value.historyNextCursor)
            assertEquals(listOf(null, "cursor-1"), api.historyCursors)

            viewModel.loadOlderApprovalHistory()
            runCurrent()
            assertEquals(listOf("event-1", "event-2", "event-3"), viewModel.state.value.approvalHistory.map { it.eventId })
            assertEquals(false, viewModel.state.value.historyHasMore)
            assertEquals(listOf(null, "cursor-1", "cursor-2"), api.historyCursors)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun failedOlderHistoryPageKeepsLoadedEventsAndRetriesSameCursor() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = StaleApprovalApi()
            api.historyPages[null] = ApprovalHistoryResponseDto(
                events = listOf(historyDto("event-1")),
                hasMore = true,
                nextCursor = "cursor-1"
            )
            api.failHistoryCursor = "cursor-1"
            val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
            val viewModel = ApprovalsViewModel(application, ApprovalRepository(api))
            runCurrent()

            viewModel.loadApprovalHistory()
            runCurrent()
            viewModel.loadOlderApprovalHistory()
            runCurrent()

            assertEquals(listOf("event-1"), viewModel.state.value.approvalHistory.map { it.eventId })
            assertEquals(true, viewModel.state.value.historyLoaded)
            assertEquals(true, viewModel.state.value.historyHasMore)
            assertEquals("cursor-1", viewModel.state.value.historyNextCursor)
            assertTrue(viewModel.state.value.historyLoadMoreError.orEmpty().contains("Older decisions"))

            api.failHistoryCursor = null
            api.historyPages["cursor-1"] = ApprovalHistoryResponseDto(
                events = listOf(historyDto("event-2")),
                hasMore = false
            )
            viewModel.loadOlderApprovalHistory()
            runCurrent()

            assertEquals(listOf("event-1", "event-2"), viewModel.state.value.approvalHistory.map { it.eventId })
            assertEquals(false, viewModel.state.value.historyHasMore)
            assertEquals(null, viewModel.state.value.historyLoadMoreError)
            assertEquals(listOf(null, "cursor-1", "cursor-1"), api.historyCursors)
        } finally {
            Dispatchers.resetMain()
        }
    }

    private class StaleApprovalApi(
        private val terminalOnRefresh: Boolean = true,
        private val expiresAt: String = "2026-10-22T12:00:00Z"
    ) : BaseFakeControlTowerApi() {
        var decisionCalls = 0
        val decisionApprovalIds = mutableListOf<String>()
        val statusApprovalIds = mutableListOf<String>()
        val historyCursors = mutableListOf<String?>()
        val historyPages = mutableMapOf<String?, ApprovalHistoryResponseDto>()
        var failHistoryCursor: String? = null
        var approvalRows: List<ApprovalRequestDto> = emptyList()
        var failApprovalRefresh = false

        override suspend fun listApprovals(): List<ApprovalRequestDto> {
            if (failApprovalRefresh) throw IOException("Approval inbox unavailable")
            return approvalRows
        }

        override suspend fun listApprovalHistory(
            limit: Int,
            cursor: String?
        ): ApprovalHistoryResponseDto {
            historyCursors += cursor
            if (cursor != null && cursor == failHistoryCursor) {
                throw IOException("History page unavailable")
            }
            return historyPages[cursor] ?: ApprovalHistoryResponseDto(limit = limit)
        }

        override suspend fun decideApproval(
            approvalId: String,
            payload: DecisionRequestDto
        ): ApprovalRequestDto {
            decisionCalls += 1
            decisionApprovalIds += approvalId
            throw IOException("Approval changed before decision commit.")
        }

        override suspend fun getApproval(approvalId: String): ApprovalRequestDto {
            statusApprovalIds += approvalId
            return approvalDto(approvalId, if (terminalOnRefresh) "DENIED" else "REQUESTED", expiresAt)
        }
    }

    private class TestApplication : Application() {
        override fun getApplicationContext(): Context = this
    }

    private companion object {
        const val APPROVAL_ID = "approval-stale-decision-123"
        val unsafe: Any = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe").run {
            isAccessible = true
            requireNotNull(get(null))
        }

        fun allocateWithoutConstructor(type: Class<*>): Any = unsafe.javaClass
            .getMethod("allocateInstance", Class::class.java)
            .invoke(unsafe, type)
            .let(::requireNotNull)
    }
}

private fun historyDto(id: String) = ApprovalHistoryEventDto(
    auditEventId = id,
    approvalId = "approval-$id",
    decision = "approve",
    reviewerLabel = "You"
)

private fun approvalDto(
    id: String,
    status: String,
    expiresAt: String = "2026-10-22T12:00:00Z"
) = ApprovalRequestDto(
    id = id,
    project_id = "project-1",
    title = "Approval request",
    summary = "Review this change",
    agent_name = "Antigravity",
    target_branch = "main",
    status = status,
    risk = RiskAssessmentDto(level = "LOW"),
    expires_at = expiresAt,
    rollback_plan = "Revert"
)

private fun staleApprovalSummary(expiresAt: String = "2026-10-22T12:00:00Z") = ApprovalSummary(
    id = "approval-stale-decision-123",
    projectName = "Project",
    title = "Approval request",
    summary = "Review this change",
    agentName = "Antigravity",
    targetBranch = "main",
    status = ApprovalStatus.REQUESTED,
    risk = RiskLevel.LOW,
    files = emptyList(),
    requiredActions = emptyList(),
    testPlan = emptyList(),
    rollbackPlan = "Revert",
    projectId = "project-1",
    expiresAt = expiresAt
)
