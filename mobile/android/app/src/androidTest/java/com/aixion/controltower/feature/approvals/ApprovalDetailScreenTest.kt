package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Rule
import org.junit.Assert.assertEquals
import org.junit.Test

class ApprovalDetailScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun unknownApprovalStatusDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(approval(ApprovalStatus.UNKNOWN), null, {}, {}, {})
        }

        composeRule.onNodeWithText("UNSUPPORTED DATA").assertExists()
        composeRule.onNodeWithText("Unsupported approval data").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun unknownApprovalRiskDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(approval(ApprovalStatus.REQUESTED).copy(risk = RiskLevel.UNKNOWN), null, {}, {}, {})
        }

        composeRule.onNodeWithText("UNSUPPORTED DATA").assertExists()
        composeRule.onNodeWithText("This request contains an unrecognized status or risk value. Decisions are disabled.").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun expiredApprovalShowsClosedStateAndDisablesEveryDecision() {
        composeRule.setContent { ApprovalDetailContent(approval(ApprovalStatus.EXPIRED), null, {}, {}, {}) }

        composeRule.onNodeWithText("Decision closed").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun actionMessageForDifferentApprovalIsNotShownOnExpiredDetail() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.EXPIRED),
                lastActionMessage = "Decision sent: approve. Request moved out of the active queue.",
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                lastActionApprovalId = "approval-that-was-just-decided"
            )
        }

        composeRule.onNodeWithText("Decision closed").assertExists()
        composeRule.onNodeWithText("Decision sent: approve. Request moved out of the active queue.")
            .assertDoesNotExist()
    }

    @Test
    fun actionMessageForCurrentApprovalIsShown() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.EXPIRED),
                lastActionMessage = "Decision failed: approval already expired.",
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                lastActionApprovalId = "approval-detail-test"
            )
        }

        composeRule.onNodeWithText("Decision failed: approval already expired.").assertExists()
    }

    @Test
    fun pendingApprovalAllowsOperatorDecisions() {
        composeRule.setContent { ApprovalDetailContent(approval(ApprovalStatus.REQUESTED), null, {}, {}, {}) }

        composeRule.onNodeWithText("READY FOR DECISION").assertExists()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun pendingApprovalWithoutExpiryDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval(ApprovalStatus.REQUESTED).copy(expiresAt = null),
                null,
                {},
                {},
                {}
            )
        }

        composeRule.onNodeWithText("Decision blocked").assertExists()
        composeRule.onNodeWithText("This request has no valid expiry. Refresh it before deciding.").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun expiredPendingApprovalDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval(ApprovalStatus.REQUESTED).copy(expiresAt = "2000-01-01T00:00:00Z"),
                null,
                {},
                {},
                {}
            )
        }

        composeRule.onNodeWithText("This request has expired. Refresh the inbox before deciding.").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun externalApprovalWithoutInvocationIdentityDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval(ApprovalStatus.REQUESTED).copy(sourceInvocationId = null),
                null,
                {},
                {},
                {}
            )
        }

        composeRule.onNodeWithText("The connector installation, session, or invocation identity is missing.").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun validManualApprovalDoesNotRequireConnectorIdentity() {
        val manual = approval(ApprovalStatus.REQUESTED).copy(
            sourceProvider = "MANUAL",
            sourceAgentId = null,
            sourceSessionId = null,
            sourceInvocationId = null,
            verifiedSource = false,
            expiresAt = Instant.now().plusSeconds(600).toString()
        )
        composeRule.setContent { ApprovalDetailContent(manual, null, {}, {}, {}) }

        composeRule.onNodeWithText("READY FOR DECISION").assertExists()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun uncertainDecisionDisablesActionsUntilServerStatusIsChecked() {
        var reconciled = false
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.REQUESTED),
                lastActionMessage = "Decision result is unknown.",
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                lastActionApprovalId = "approval-detail-test",
                decisionNeedsReconciliation = true,
                onReconcileDecision = { reconciled = true }
            )
        }

        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
        composeRule.onNodeWithText("Check server status").performClick()
        composeRule.runOnIdle { assertEquals(true, reconciled) }
    }

    @Test
    fun approvalDetailShowsInvocationIdentityAndRiskRationale() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            projectId = "project-production",
            sourceAgentId = "agent-antigravity-01",
            sourceSessionId = "session-codex-42",
            sourceInvocationId = "antigravity:session-codex-42:tool-19",
            expiresAt = "2026-10-21T12:30:00Z",
            verifiedSource = true,
            riskReasons = listOf("Antigravity command can cause broad side effects.")
        )
        composeRule.setContent {
            ApprovalDetailContent(request, null, {}, {}, {})
        }

        composeRule.onNodeWithText("Approval ID: approval-detail-test", substring = true).assertExists()
        composeRule.onNodeWithText("Project ID: project-production", substring = true).assertExists()
        composeRule.onNodeWithText("Source agent ID: agent-antigravity-01", substring = true).assertExists()
        composeRule.onNodeWithText("Session ID: session-codex-42", substring = true).assertExists()
        composeRule.onNodeWithText(
            "Source invocation ID: antigravity:session-codex-42:tool-19",
            substring = true
        ).assertExists()
        composeRule.onNodeWithText("Expires at: 2026-10-21T12:30:00Z", substring = true).assertExists()
        composeRule.onNodeWithText("Source verified: Yes", substring = true).assertExists()
        composeRule.onNodeWithText(
            "Antigravity command can cause broad side effects.",
            substring = true
        ).assertExists()
    }

    @Test
    fun approvalDeepLinkLoadingStateDoesNotExposeDecisionActionsForAnotherApproval() {
        composeRule.setContent {
            ApprovalDetailEmptyContent(loading = true, message = null, onRetry = null)
        }

        composeRule.onNodeWithText("Loading requested approval…").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun approvalDeepLinkFailureShowsRetryInsteadOfFallingBackToAnotherApproval() {
        var retryCount = 0
        composeRule.setContent {
            ApprovalDetailEmptyContent(
                loading = false,
                message = "Failed to open approval approval_missing: not found",
                onRetry = { retryCount += 1 }
            )
        }

        composeRule.onNodeWithTag("approval-detail-open-error").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Retry approval load").performClick()
        assertEquals(1, retryCount)
    }
}

private fun approval(status: ApprovalStatus) = ApprovalSummary(
    id = "approval-detail-test",
    projectName = "Test project",
    title = "Approval detail test",
    summary = "A detail screen state test.",
    agentName = "Antigravity",
    targetBranch = "main",
    status = status,
    risk = RiskLevel.LOW,
    files = listOf(FileChangeSummary("README.md", "update", "+test", RiskLevel.LOW)),
    requiredActions = emptyList(),
    testPlan = listOf("Run focused tests"),
    rollbackPlan = "Revert the test change",
    sourceProvider = "ANTIGRAVITY",
    sourceAgentId = "agent-detail-test",
    sourceSessionId = "session-detail-test",
    sourceInvocationId = "invocation-detail-test",
    expiresAt = Instant.now().plusSeconds(600).toString(),
    verifiedSource = true
)
