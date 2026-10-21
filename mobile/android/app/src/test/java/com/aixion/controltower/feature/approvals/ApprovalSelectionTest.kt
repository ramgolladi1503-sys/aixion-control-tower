package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ApprovalSelectionTest {
    @Test
    fun inboxPlacesPendingWorkBeforeResolvedAndUnsupportedItems() {
        val approved = approval("approval_approved").copy(status = ApprovalStatus.APPROVED)
        val olderPending = approval("approval_pending_older")
        val blocked = approval("approval_blocked").copy(status = ApprovalStatus.BLOCKED)
        val unsupported = approval("approval_unsupported").copy(status = ApprovalStatus.UNKNOWN)
        val newerPending = approval("approval_pending_newer").copy(status = ApprovalStatus.PENDING_REVIEW)

        val ordered = orderApprovalsForInbox(
            listOf(approved, olderPending, blocked, unsupported, newerPending)
        )

        assertEquals(
            listOf(olderPending, newerPending, blocked, unsupported, approved).map { it.id },
            ordered.map { it.id }
        )
    }

    @Test
    fun refreshSelectsRequestedApprovalInsteadOfFirstInboxItem() {
        val unrelated = approval("approval_unrelated")
        val requested = approval("approval_requested")
        val current = ApprovalsUiState(
            approvals = listOf(unrelated),
            openingApprovalId = requested.id,
            detailTargetApprovalId = requested.id
        )

        val refreshed = reconcileApprovalsAfterRefresh(current, listOf(unrelated, requested))

        assertEquals(requested.id, refreshed.selectedApproval?.id)
        assertNull(refreshed.openingApprovalId)
        assertEquals(requested.id, refreshed.detailTargetApprovalId)
    }

    @Test
    fun refreshNeverSubstitutesAnUnrelatedApprovalWhileRequestedApprovalLoads() {
        val unrelated = approval("approval_unrelated")
        val current = ApprovalsUiState(
            approvals = listOf(unrelated),
            openingApprovalId = "approval_missing",
            detailTargetApprovalId = "approval_missing"
        )

        val refreshed = reconcileApprovalsAfterRefresh(current, listOf(unrelated))

        assertNull(refreshed.selectedApproval)
        assertEquals("approval_missing", refreshed.openingApprovalId)
        assertEquals("approval_missing", refreshed.detailTargetApprovalId)
    }

    @Test
    fun normalInboxRefreshStillSelectsFirstApprovalWhenNoTargetWasRequested() {
        val first = approval("approval_first")
        val refreshed = reconcileApprovalsAfterRefresh(ApprovalsUiState(), listOf(first))

        assertEquals(first.id, refreshed.selectedApproval?.id)
        assertNull(refreshed.detailTargetApprovalId)
    }
}

private fun approval(id: String) = ApprovalSummary(
    id = id,
    projectName = "Project",
    title = id,
    summary = "Summary",
    agentName = "Antigravity",
    targetBranch = "main",
    status = ApprovalStatus.REQUESTED,
    risk = RiskLevel.LOW,
    files = emptyList(),
    requiredActions = emptyList(),
    testPlan = emptyList(),
    rollbackPlan = "Revert"
)
