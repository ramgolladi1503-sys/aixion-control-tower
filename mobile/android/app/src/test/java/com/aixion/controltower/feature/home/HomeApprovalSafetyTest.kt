package com.aixion.controltower.feature.home

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertEquals
import org.junit.Test

class HomeApprovalSafetyTest {
    @Test
    fun unsupportedApprovalDataIsVisibleAsAttentionAndNeverCountsAsActionable() {
        val state = HomeUiState(
            loading = false,
            approvals = listOf(
                approval("unknown_status", ApprovalStatus.UNKNOWN, RiskLevel.LOW),
                approval("unknown_risk", ApprovalStatus.REQUESTED, RiskLevel.UNKNOWN)
            )
        )

        assertEquals(2, state.unsupportedApprovals.size)
        assertEquals(0, state.actionRequiredCount)
        assertEquals(1, state.pendingCount)
    }

    @Test
    fun pendingCountIncludesCurrentAndLegacyPendingStatusesOnly() {
        val state = HomeUiState(
            loading = false,
            approvals = listOf(
                approval("requested", ApprovalStatus.REQUESTED, RiskLevel.LOW),
                approval("legacy_pending", ApprovalStatus.PENDING_REVIEW, RiskLevel.MEDIUM),
                approval("approved", ApprovalStatus.APPROVED, RiskLevel.LOW),
                approval("expired", ApprovalStatus.EXPIRED, RiskLevel.LOW)
            )
        )

        assertEquals(2, state.pendingCount)
    }
}

private fun approval(id: String, status: ApprovalStatus, risk: RiskLevel) = ApprovalSummary(
    id = id,
    projectName = "Test project",
    title = "Unsupported approval",
    summary = "The app must not assume unknown server values are safe.",
    agentName = "Antigravity",
    targetBranch = "main",
    status = status,
    risk = risk,
    files = listOf(FileChangeSummary("README.md", "update", "diff", risk)),
    requiredActions = emptyList(),
    testPlan = emptyList(),
    rollbackPlan = ""
)
