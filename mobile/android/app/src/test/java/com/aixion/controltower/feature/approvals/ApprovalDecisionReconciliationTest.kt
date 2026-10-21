package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.ApprovalStatus
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test

class ApprovalDecisionReconciliationTest {
    @Test
    fun onlyExactTerminalDecisionConfirmsTheRequestedAction() {
        assertTrue(decisionMatchesStatus("approve", ApprovalStatus.APPROVED))
        assertTrue(decisionMatchesStatus("reject", ApprovalStatus.DENIED))
        assertTrue(decisionMatchesStatus("revise", ApprovalStatus.REVISION_REQUESTED))

        assertFalse(decisionMatchesStatus("approve", ApprovalStatus.REQUESTED))
        assertFalse(decisionMatchesStatus("approve", ApprovalStatus.DENIED))
        assertFalse(decisionMatchesStatus("unknown", ApprovalStatus.APPROVED))
        assertFalse(decisionMatchesStatus(null, ApprovalStatus.APPROVED))
    }

    @Test
    fun interruptedDecisionRemainsBlockedUntilServerReconciliation() {
        val inFlight = ApprovalsUiState(
            decisionInFlight = true,
            lastActionApprovalId = "approval-cancelled"
        )

        val unresolved = decisionOutcomeUnknown(
            current = inFlight,
            approvalId = "approval-cancelled",
            decision = "approve",
            message = "The request was interrupted; check the server before retrying."
        )

        assertFalse(unresolved.decisionInFlight)
        assertTrue(unresolved.decisionNeedsReconciliation)
        assertEquals("approve", unresolved.decisionBeingReconciled)
        assertEquals("approval-cancelled", unresolved.lastActionApprovalId)
        assertFalse(canStartApprovalDecision(unresolved))
        assertTrue(canStartApprovalDecision(unresolved.copy(decisionNeedsReconciliation = false)))
    }
}
