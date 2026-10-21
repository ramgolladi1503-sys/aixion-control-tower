package com.aixion.controltower.feature.approvals

import android.app.Application
import android.content.Context
import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.RiskAssessmentDto
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.data.repository.ApprovalRepository
import com.aixion.controltower.data.repository.BaseFakeControlTowerApi
import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ApprovalDecisionViewModelTest {
    @Test
    fun quickDuplicateTapsDispatchOnlyOneDecision() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = RecordingApprovalApi()
            val viewModel = newViewModel(api)
            runCurrent()
            viewModel.selectApproval(approvalSummary(REQUEST_ID))

            viewModel.decide("approve", "reviewed")
            viewModel.decide("approve", "reviewed")
            runCurrent()

            assertEquals("A rapid second tap must not issue another server decision.", 1, api.decisionCalls)
            assertEquals(listOf(REQUEST_ID), api.decisionApprovalIds)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun ambiguousDecisionStaysBlockedUntilSameApprovalIsReconciled() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = RecordingApprovalApi().apply {
                decideResult = { _, _ -> throw IOException("decision response lost") }
                getResult = { id, callNumber ->
                    if (callNumber == 1) throw IOException("status endpoint unavailable")
                    approvalDto(id, "APPROVED")
                }
            }
            val viewModel = newViewModel(api)
            runCurrent()
            viewModel.selectApproval(approvalSummary(REQUEST_ID))

            viewModel.decide("approve", "reviewed")
            runCurrent()

            assertEquals(1, api.decisionCalls)
            assertEquals(listOf(REQUEST_ID), api.decisionApprovalIds)
            assertEquals(listOf(REQUEST_ID), api.statusApprovalIds)
            assertTrue(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(REQUEST_ID, viewModel.state.value.lastActionApprovalId)
            assertEquals(ApprovalStatus.REQUESTED, viewModel.state.value.selectedApproval?.status)
            assertTrue(viewModel.state.value.approvals.none { it.status == ApprovalStatus.APPROVED })
            assertFalse(canStartApprovalDecision(viewModel.state.value))

            // A further tap while the response remains ambiguous must stay local.
            viewModel.decide("approve", "reviewed")
            runCurrent()
            assertEquals(1, api.decisionCalls)
            assertTrue(viewModel.state.value.decisionNeedsReconciliation)

            viewModel.reconcileDecisionStatus()
            runCurrent()

            assertEquals(listOf(REQUEST_ID, REQUEST_ID), api.statusApprovalIds)
            assertFalse(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(REQUEST_ID, viewModel.state.value.lastActionApprovalId)
            assertEquals(ApprovalStatus.APPROVED, viewModel.state.value.approvals.single().status)
            assertTrue(canStartApprovalDecision(viewModel.state.value))
            assertEquals(1, api.decisionCalls)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun reconciliationShowingStillPendingDoesNotResubmitDecision() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = RecordingApprovalApi().apply {
                decideResult = { _, _ -> throw IOException("decision response lost") }
                getResult = { id, callNumber ->
                    if (callNumber == 1) throw IOException("automatic status check failed")
                    approvalDto(id, "REQUESTED")
                }
            }
            val viewModel = newViewModel(api)
            runCurrent()
            viewModel.selectApproval(approvalSummary(REQUEST_ID))

            viewModel.decide("approve", "reviewed")
            runCurrent()
            assertTrue(viewModel.state.value.decisionNeedsReconciliation)

            viewModel.reconcileDecisionStatus()
            runCurrent()

            assertEquals(listOf(REQUEST_ID, REQUEST_ID), api.statusApprovalIds)
            assertEquals(listOf(REQUEST_ID), api.decisionApprovalIds)
            assertEquals(1, api.decisionCalls)
            assertFalse(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(ApprovalStatus.REQUESTED, viewModel.state.value.selectedApproval?.status)
            assertEquals(REQUEST_ID, viewModel.state.value.lastActionApprovalId)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun reconciliationShowingConflictingTerminalStateDoesNotResubmitDecision() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = RecordingApprovalApi().apply {
                decideResult = { _, _ -> throw IOException("decision response lost") }
                getResult = { id, callNumber ->
                    if (callNumber == 1) throw IOException("automatic status check failed")
                    approvalDto(id, "DENIED")
                }
            }
            val viewModel = newViewModel(api)
            runCurrent()
            viewModel.selectApproval(approvalSummary(REQUEST_ID))

            viewModel.decide("approve", "reviewed")
            runCurrent()
            viewModel.reconcileDecisionStatus()
            runCurrent()

            assertEquals(listOf(REQUEST_ID, REQUEST_ID), api.statusApprovalIds)
            assertEquals(listOf(REQUEST_ID), api.decisionApprovalIds)
            assertEquals(1, api.decisionCalls)
            assertFalse(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(ApprovalStatus.DENIED, viewModel.state.value.selectedApproval?.status)
            assertTrue(viewModel.state.value.lastActionMessage.orEmpty().contains("No decision retry was sent."))
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun repeatedReconciliationFailuresKeepDecisionUnknownAndBlocked() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = RecordingApprovalApi().apply {
                decideResult = { _, _ -> throw IOException("decision response lost") }
                getResult = { _, _ -> throw IOException("status endpoint unavailable") }
            }
            val viewModel = newViewModel(api)
            runCurrent()
            viewModel.selectApproval(approvalSummary(REQUEST_ID))

            viewModel.decide("approve", "reviewed")
            runCurrent()
            assertTrue(viewModel.state.value.decisionNeedsReconciliation)

            viewModel.reconcileDecisionStatus()
            runCurrent()
            viewModel.decide("approve", "reviewed")
            runCurrent()

            assertEquals(listOf(REQUEST_ID, REQUEST_ID), api.statusApprovalIds)
            assertEquals(listOf(REQUEST_ID), api.decisionApprovalIds)
            assertEquals(1, api.decisionCalls)
            assertTrue(viewModel.state.value.decisionNeedsReconciliation)
            assertFalse(canStartApprovalDecision(viewModel.state.value))
            assertEquals(REQUEST_ID, viewModel.state.value.lastActionApprovalId)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun mismatchedDecisionResponseRequiresSameRequestStatusAndNeverRetries() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = RecordingApprovalApi().apply {
                decideResult = { id, _ -> approvalDto(id, "DENIED") }
                getResult = { id, callNumber ->
                    if (callNumber == 1) throw IOException("automatic status check failed")
                    approvalDto(id, "DENIED")
                }
            }
            val viewModel = newViewModel(api)
            runCurrent()
            viewModel.selectApproval(approvalSummary(REQUEST_ID))

            viewModel.decide("approve", "reviewed")
            runCurrent()
            assertTrue(viewModel.state.value.decisionNeedsReconciliation)

            viewModel.reconcileDecisionStatus()
            runCurrent()

            assertEquals(listOf(REQUEST_ID), api.decisionApprovalIds)
            assertEquals(1, api.decisionCalls)
            assertEquals(listOf(REQUEST_ID, REQUEST_ID), api.statusApprovalIds)
            assertFalse(viewModel.state.value.decisionNeedsReconciliation)
            assertEquals(ApprovalStatus.DENIED, viewModel.state.value.selectedApproval?.status)
            assertEquals(REQUEST_ID, viewModel.state.value.lastActionApprovalId)
            assertTrue(viewModel.state.value.lastActionMessage.orEmpty().contains("No decision retry was sent."))
        } finally {
            Dispatchers.resetMain()
        }
    }

    private fun newViewModel(api: ControlTowerApi): ApprovalsViewModel {
        val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
        return ApprovalsViewModel(application, ApprovalRepository(api))
    }

    private class RecordingApprovalApi : BaseFakeControlTowerApi() {
        var decisionCalls = 0
        val decisionApprovalIds = mutableListOf<String>()
        val statusApprovalIds = mutableListOf<String>()
        var decideResult: suspend (String, DecisionRequestDto) -> ApprovalRequestDto = { id, payload ->
            approvalDto(id, when (payload.decision) {
                "approve" -> "APPROVED"
                "reject" -> "DENIED"
                "revise" -> "REVISION_REQUESTED"
                else -> "REQUESTED"
            })
        }
        var getResult: suspend (String, Int) -> ApprovalRequestDto = { id, _ -> approvalDto(id, "REQUESTED") }

        override suspend fun listApprovals(): List<ApprovalRequestDto> = emptyList()

        override suspend fun getApproval(approvalId: String): ApprovalRequestDto {
            statusApprovalIds += approvalId
            return getResult(approvalId, statusApprovalIds.size)
        }

        override suspend fun decideApproval(
            approvalId: String,
            payload: DecisionRequestDto
        ): ApprovalRequestDto {
            decisionCalls += 1
            decisionApprovalIds += approvalId
            return decideResult(approvalId, payload)
        }
    }

    private class TestApplication : Application() {
        override fun getApplicationContext(): Context = this
    }

    private companion object {
        const val REQUEST_ID = "approval-request-123"
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

private fun approvalDto(id: String, status: String) = ApprovalRequestDto(
    id = id,
    project_id = "project-1",
    title = "Approval request",
    summary = "Review this change",
    target_branch = "main",
    status = status,
    risk = RiskAssessmentDto(level = "LOW")
)

private fun approvalSummary(id: String) = ApprovalSummary(
    id = id,
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
    rollbackPlan = "Revert"
)
