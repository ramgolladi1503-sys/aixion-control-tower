package com.aixion.controltower.feature.home

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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

    @Test
    fun failedRefreshPreservesLastSuccessfulSyncAndCachedRecords() {
        val syncedAt = Instant.parse("2026-10-22T05:30:00Z")
        val existingApproval = approval("existing", ApprovalStatus.REQUESTED, RiskLevel.LOW)
        val beforeFailure = HomeUiState(
            loading = false,
            projects = emptyList(),
            approvals = listOf(existingApproval),
            lastSuccessfulSyncAt = syncedAt
        )

        val afterFailure = homeRefreshFailureState(beforeFailure, "Network unavailable")

        assertFalse(afterFailure.loading)
        assertFalse(afterFailure.refreshing)
        assertTrue(afterFailure.hasError)
        assertEquals(syncedAt, afterFailure.lastSuccessfulSyncAt)
        assertEquals(listOf(existingApproval), afterFailure.approvals)
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
