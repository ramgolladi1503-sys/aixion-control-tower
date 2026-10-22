package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performClick
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Rule
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ApprovalDetailScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun unknownApprovalStatusDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(approval(ApprovalStatus.UNKNOWN), null, {}, {}, {})
        }

        composeRule.onNodeWithText("Unsupported data").assertExists()
        composeRule.onNodeWithText("Unsupported approval data").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun unknownApprovalRiskDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(approval(ApprovalStatus.REQUESTED).copy(risk = RiskLevel.UNKNOWN), null, {}, {}, {})
        }

        composeRule.onNodeWithText("Unsupported data").assertExists()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Unsupported approval data"))
        composeRule.onNodeWithText("Unsupported approval data").assertExists()
        composeRule.onNodeWithText(
            "The backend returned a status or risk value this app does not recognize. " +
                "Decisions are disabled until the app and backend versions are compatible."
        ).assertExists()
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

        composeRule.onNodeWithText("Ready for decision").assertExists()
        composeRule.onNodeWithText("Low").assertExists()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasText("+test"))
        composeRule.onNodeWithText("+test").assertExists()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun decisionActionsAreStackedFullWidthTargets() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                ApprovalDetailContent(approval(ApprovalStatus.REQUESTED), null, {}, {}, {})
            }
        }

        val rejectBounds = composeRule.onNodeWithText("Reject").fetchSemanticsNode().boundsInRoot
        val approveBounds = composeRule.onNodeWithText("Approve request").fetchSemanticsNode().boundsInRoot

        assertTrue("Decision buttons should be separate vertical targets", rejectBounds.bottom < approveBounds.top)
        assertTrue("Reject should remain a 48dp or taller target", rejectBounds.height >= 48f)
        assertTrue("Approve should remain a 48dp or taller target", approveBounds.height >= 48f)
        assertTrue(
            "Decision buttons should use the same full-width size",
            kotlin.math.abs(rejectBounds.width - approveBounds.width) < 1f
        )
    }

    @Test
    fun decisionProgressStatusUsesPoliteLiveRegionAndUpdatesInPlace() {
        val decisionInFlight = androidx.compose.runtime.mutableStateOf(false)
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.REQUESTED),
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                decisionInFlight = decisionInFlight.value
            )
        }

        val status = composeRule.onNodeWithTag("approval-decision-status")
        status.assertTextContains("Approval is available.")
        assertEquals(
            LiveRegionMode.Polite,
            status.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )

        composeRule.runOnIdle { decisionInFlight.value = true }

        status.assertTextContains("Sending the decision and waiting for the server.")
        assertEquals(
            LiveRegionMode.Polite,
            status.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
    }

    @Test
    fun approvalHeroStatusAndRiskBadgesWrapAtNarrowWidth() {
        composeRule.setContent {
            Box(modifier = Modifier.width(220.dp).height(900.dp)) {
                ApprovalDetailContent(
                    approval(ApprovalStatus.REQUESTED).copy(risk = RiskLevel.HIGH),
                    null,
                    {},
                    {},
                    {}
                )
            }
        }

        val statusBounds = composeRule.onNodeWithText("Ready for decision").fetchSemanticsNode().boundsInRoot
        val riskBounds = composeRule.onNodeWithText("High").fetchSemanticsNode().boundsInRoot
        assertTrue(
            "Approval status and risk badges should move to separate rows at narrow widths",
            statusBounds.bottom < riskBounds.top
        )
    }

    @Test
    fun decisionContextStacksLabelsAndValuesAtLargeFontScale() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(390.dp).height(1_200.dp)) {
                    ApprovalDetailContent(
                        approval(ApprovalStatus.REQUESTED),
                        null,
                        {},
                        {},
                        {}
                    )
                }
            }
        }

        val projectLabel = composeRule.onNodeWithText("Project", useUnmergedTree = true)
            .fetchSemanticsNode().boundsInRoot
        val projectValue = composeRule.onNodeWithText("Production workspace", useUnmergedTree = true)
            .fetchSemanticsNode().boundsInRoot
        assertTrue(
            "At 200% font scale, decision-critical values should follow their labels vertically",
            projectLabel.bottom < projectValue.top
        )
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

        composeRule.onNodeWithText("Ready for decision").assertExists()
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
    fun approvalDetailKeepsSafetyContextVisibleAndHidesRawIdsUntilExpanded() {
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

        composeRule.onNodeWithText("Risk rationale").assertExists()
        composeRule.onNodeWithText("ANTIGRAVITY · Verified source").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("main").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Show technical details").performScrollTo().assertIsDisplayed()
        composeRule.onAllNodesWithText("Approval ID: approval-detail-test", substring = true).assertCountEquals(0)
        composeRule.onNodeWithText("Show technical details").performClick()
        composeRule.onNodeWithText("Approval ID: approval-detail-test", substring = true).assertExists()
        composeRule.onNodeWithText("Project ID: project-production", substring = true).assertExists()
        composeRule.onNodeWithText("Source agent ID: agent-antigravity-01", substring = true).assertExists()
        composeRule.onNodeWithText("Session ID: session-codex-42", substring = true).assertExists()
        composeRule.onNodeWithText("Source invocation ID: antigravity:session-codex-42:tool-19", substring = true).assertExists()
        composeRule.onNodeWithText("Expires at: 2026-10-21T12:30:00Z", substring = true).assertExists()
        composeRule.onNodeWithText("Source verified: Yes", substring = true).assertExists()
        composeRule.onNodeWithText(
            "Antigravity command can cause broad side effects.",
            substring = true
        ).assertExists()
    }

    @Test
    fun approvalDecisionContextIsReachableFromSummaryBeforeDecision() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            projectName = "Payments service",
            targetBranch = "release/production",
            sourceProvider = "ANTIGRAVITY",
            sourceInvocationId = "antigravity:session-42:tool-19",
            verifiedSource = true,
            expiresAt = "2026-10-22T14:30:00Z",
            riskReasons = listOf(
                "Command may modify production configuration.",
                "Command runs with workspace write access."
            )
        )
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        composeRule.onNodeWithText("Decision context").assertExists()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
        composeRule.onNodeWithText("Command may modify production configuration.", substring = true)
            .performScrollTo()
        composeRule.onNodeWithText("Command may modify production configuration.", substring = true)
            .assertIsDisplayed()
        composeRule.onNodeWithText("Payments service").performScrollTo()
        composeRule.onNodeWithText("Payments service").assertIsDisplayed()
        composeRule.onNodeWithText("release/production").assertIsDisplayed()
        composeRule.onNodeWithText("Command runs with workspace write access.", substring = true)
            .performScrollTo()
        composeRule.onNodeWithText("Command runs with workspace write access.", substring = true)
            .assertIsDisplayed()
        composeRule.onNodeWithText("ANTIGRAVITY · Verified source").performScrollTo()
        composeRule.onNodeWithText("ANTIGRAVITY · Verified source").assertIsDisplayed()
        composeRule.onNodeWithText("Show technical details").performScrollTo().performClick()
        composeRule.onNodeWithText("antigravity:session-42:tool-19").assertIsDisplayed()
        composeRule.onNodeWithText("2026-10-22T14:30:00Z").performScrollTo()
        composeRule.onNodeWithText("2026-10-22T14:30:00Z").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
    }

    @Test
    fun approvalDecisionContextLabelsMissingFieldsInsteadOfHidingThem() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            projectName = "",
            targetBranch = "",
            sourceInvocationId = null,
            verifiedSource = false,
            expiresAt = "",
            riskReasons = emptyList()
        )
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        composeRule.onAllNodesWithText("Not provided").assertCountEquals(3)
        composeRule.onNodeWithText("ANTIGRAVITY · Source not verified").assertExists()
        composeRule.onNodeWithText("No risk rationale was supplied.").assertExists()
        composeRule.onNodeWithText("Show technical details").performScrollTo().performClick()
        composeRule.onNodeWithText("Source invocation ID: Not provided", substring = true).assertExists()
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
