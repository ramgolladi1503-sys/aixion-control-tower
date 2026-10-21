package com.aixion.controltower.feature.approvals

import android.app.Application
import android.content.Context
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
    fun staleApproveReconcilesToServerDeniedStateWithoutRetry() = runTest {
        Dispatchers.setMain(StandardTestDispatcher(testScheduler))
        try {
            val api = StaleApprovalApi()
            val application = allocateWithoutConstructor(TestApplication::class.java) as TestApplication
            val viewModel = ApprovalsViewModel(application, ApprovalRepository(api))
            runCurrent()
            viewModel.selectApproval(staleApprovalSummary())

            viewModel.decide("approve", "reviewed after inbox refresh")
            runCurrent()

            assertEquals(1, api.decisionCalls)
            assertEquals(listOf(APPROVAL_ID), api.decisionApprovalIds)
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
                    .contains("No retry was sent.")
            )

            viewModel.decide("approve", "retry must remain blocked for the terminal request")
            runCurrent()
            assertEquals("A terminal request must not receive a second decision.", 1, api.decisionCalls)
        } finally {
            Dispatchers.resetMain()
        }
    }

    private class StaleApprovalApi : BaseFakeControlTowerApi() {
        var decisionCalls = 0
        val decisionApprovalIds = mutableListOf<String>()
        val statusApprovalIds = mutableListOf<String>()

        override suspend fun listApprovals(): List<ApprovalRequestDto> = emptyList()

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
            return approvalDto(approvalId, "DENIED")
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

private fun approvalDto(id: String, status: String) = ApprovalRequestDto(
    id = id,
    project_id = "project-1",
    title = "Approval request",
    summary = "Review this change",
    target_branch = "main",
    status = status,
    risk = RiskAssessmentDto(level = "LOW")
)

private fun staleApprovalSummary() = ApprovalSummary(
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
    rollbackPlan = "Revert"
)
