package com.aixion.controltower

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.feature.approvals.ApprovalsUiState
import org.junit.Assert.assertEquals
import org.junit.Test

class ApprovalNavigationBadgeTest {
    @Test
    fun badgeCountUsesOnlyCurrentActionableApprovals() {
        val state = ApprovalsUiState(
            loading = false,
            approvals = listOf(
                approval("pending", ApprovalStatus.REQUESTED, RiskLevel.MEDIUM),
                approval("blocked", ApprovalStatus.REQUESTED, RiskLevel.BLOCKED),
                approval("unknown", ApprovalStatus.UNKNOWN, RiskLevel.MEDIUM),
                approval("approved", ApprovalStatus.APPROVED, RiskLevel.LOW)
            )
        )

        assertEquals(1, pendingApprovalNavigationCount(state))
    }

    @Test
    fun badgeCountFailsClosedWhileSnapshotIsUnverified() {
        val approvals = listOf(approval("pending", ApprovalStatus.REQUESTED, RiskLevel.MEDIUM))

        assertEquals(0, pendingApprovalNavigationCount(ApprovalsUiState(loading = true, approvals = approvals)))
        assertEquals(0, pendingApprovalNavigationCount(ApprovalsUiState(loading = false, refreshing = true, approvals = approvals)))
        assertEquals(0, pendingApprovalNavigationCount(ApprovalsUiState(loading = false, errorMessage = "offline", approvals = approvals)))
    }

    private fun approval(id: String, status: ApprovalStatus, risk: RiskLevel) = ApprovalSummary(
        id = id,
        projectName = "Project",
        title = "Approval $id",
        summary = "Review request",
        agentName = "Antigravity",
        targetBranch = "main",
        status = status,
        risk = risk,
        files = emptyList(),
        requiredActions = emptyList(),
        testPlan = emptyList(),
        rollbackPlan = ""
    )
}
