package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.formatApprovalExpiry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ApprovalInboxFilterTest {
    @Test
    fun approvalExpiryLabelUsesLocalTimeOffsetAndFailsClosedForMissingOrInvalidValues() {
        val formatted = formatApprovalExpiry("2026-10-22T10:00:00Z")

        assertTrue(formatted.startsWith("Expires "))
        assertTrue(Regex("\\((?:Z|[+-]\\d{2}:\\d{2})\\)$").containsMatchIn(formatted))
        assertEquals("Expiry unavailable", formatApprovalExpiry(null))
        assertEquals("Expiry unavailable", formatApprovalExpiry(" "))
        assertEquals("Expiry unavailable", formatApprovalExpiry("not-a-timestamp"))
    }

    @Test
    fun needsReviewIncludesOnlyActionablePendingStatesAndSortsByExpiry() {
        val pending = approval("pending", ApprovalStatus.PENDING_REVIEW).copy(expiresAt = "2026-10-22T11:00:00Z")
        val requested = approval("requested", ApprovalStatus.REQUESTED).copy(expiresAt = "2026-10-22T10:00:00Z")
        val blocked = approval("blocked", ApprovalStatus.BLOCKED)
        val revision = approval("revision", ApprovalStatus.REVISION_REQUESTED)
        val blockedRisk = approval("blocked_risk", ApprovalStatus.REQUESTED).copy(risk = RiskLevel.BLOCKED)
        val approved = approval("approved", ApprovalStatus.APPROVED)
        val expired = approval("expired", ApprovalStatus.EXPIRED)

        val visible = filterApprovalsForInbox(
            listOf(expired, approved, blocked, requested, revision, pending, blockedRisk),
            ApprovalInboxSection.NEEDS_REVIEW
        )

        assertEquals(listOf("requested", "pending"), visible.map { it.id })
    }

    @Test
    fun pendingWithoutExpirySortsAfterKnownExpiry() {
        val unknownExpiry = approval("unknown_expiry", ApprovalStatus.REQUESTED)
        val knownExpiry = approval("known_expiry", ApprovalStatus.PENDING_REVIEW)
            .copy(expiresAt = "2026-10-22T12:00:00Z")

        assertEquals(
            listOf("known_expiry", "unknown_expiry"),
            filterApprovalsForInbox(
                listOf(unknownExpiry, knownExpiry),
                ApprovalInboxSection.NEEDS_REVIEW
            ).map { it.id }
        )
    }

    @Test
    fun historyIsNeverDerivedFromMutableApprovalStatuses() {
        val mutableApprovals = listOf(
            approval("current-approved", ApprovalStatus.APPROVED),
            approval("current-denied", ApprovalStatus.DENIED),
            approval("expired", ApprovalStatus.EXPIRED)
        )

        assertTrue(filterApprovalsForInbox(mutableApprovals, ApprovalInboxSection.HISTORY).isEmpty())
        assertEquals(
            listOf("event-reject"),
            filterApprovalHistory(
                listOf(historyEvent("event-reject", "reject"), historyEvent("event-approve", "approve")),
                ApprovalHistoryFilter.REJECTED
            ).map { it.eventId }
        )
        assertEquals(
            "Denied",
            decisionHistoryLabel(historyEvent("event-reject", "reject").decision)
        )
    }

    @Test
    fun unknownStatusesAndRiskRemainInSeparateDisabledSetAcrossFilters() {
        val unknownStatus = approval("unknown_status", ApprovalStatus.UNKNOWN)
        val unknownRisk = approval("unknown_risk", ApprovalStatus.APPROVED).copy(risk = RiskLevel.UNKNOWN)
        val blockedRisk = approval("blocked_risk", ApprovalStatus.REQUESTED).copy(risk = RiskLevel.BLOCKED)
        val recognized = approval("approved", ApprovalStatus.APPROVED)
        val all = listOf(unknownStatus, unknownRisk, blockedRisk, recognized)

        assertEquals(listOf("unknown_status", "unknown_risk"), all
            .filter(::isUnsupportedInboxApproval)
            .map { it.id })
        assertTrue(
            filterApprovalsForInbox(all, ApprovalInboxSection.NEEDS_REVIEW).isEmpty()
        )
        assertTrue(filterApprovalsForInbox(all, ApprovalInboxSection.HISTORY).isEmpty())
    }

    @Test
    fun inboxSelectionIsDisabledUntilRefreshAndBackendSyncAreComplete() {
        val fresh = ApprovalsUiState(loading = false)
        assertTrue(canSelectApprovalFromInbox(fresh))
        assertTrue(!canSelectApprovalFromInbox(fresh.copy(refreshing = true)))
        assertTrue(!canSelectApprovalFromInbox(fresh.copy(loading = true)))
        assertTrue(!canSelectApprovalFromInbox(fresh.copy(errorMessage = "sync failed")))
    }
}

private fun historyEvent(id: String, decision: String) = ApprovalHistoryEvent(
    eventId = id,
    approvalId = "approval-$id",
    decision = decision,
    reason = "reviewed",
    decidedAt = "2026-10-22T10:00:00Z",
    reviewerLabel = "You",
    requestSnapshotVerified = false,
    requestSnapshot = null
)

private fun approval(id: String, status: ApprovalStatus) = ApprovalSummary(
    id = id,
    projectName = "Project",
    title = id,
    summary = "Approval summary",
    agentName = "Antigravity",
    targetBranch = "main",
    status = status,
    risk = RiskLevel.LOW,
    files = listOf(
        FileChangeSummary("src/App.kt", "modify", "diff", RiskLevel.LOW)
    ),
    requiredActions = emptyList(),
    testPlan = emptyList(),
    rollbackPlan = "Revert"
)
