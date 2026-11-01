package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeUp
import androidx.compose.ui.platform.testTag
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsProperties.ContentDescription
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
        composeRule.onNodeWithText("Closed").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Reject").assertDoesNotExist()
    }

    @Test
    fun blockedApprovalExplainsWhyItCannotBeDecided() {
        composeRule.setContent {
            ApprovalDetailContent(approval(ApprovalStatus.BLOCKED), null, {}, {}, {})
        }

        composeRule.onNodeWithText("Blocked").assertExists()
        composeRule.onNodeWithText("Approval blocked").assertExists()
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
    fun providerAndMcpToolApprovalsUseExplicitExactActionSection() {
        val toolRequests = listOf(
            Triple("ANTIGRAVITY", "tool_call", "antigravity://tool/run_command"),
            Triple("CODEX", "tool_call", "codex://tool/shell"),
            Triple("OTHER", "mcp_tool_call", "mcp://workspace/update")
        )
        val request = mutableStateOf(approval(ApprovalStatus.REQUESTED))
        composeRule.setContent { ApprovalDetailContent(request.value, null, {}, {}, {}) }

        toolRequests.forEach { (provider, changeType, path) ->
            request.value = approval(ApprovalStatus.REQUESTED).copy(
                sourceProvider = provider,
                files = listOf(
                    FileChangeSummary(
                        path = path,
                        changeType = changeType,
                        diff = "{\"command\":\"echo exact\"}",
                        risk = RiskLevel.HIGH
                    )
                )
            )
            composeRule.waitForIdle()

            composeRule.onNodeWithTag("approval-exact-tool-request").assertExists()
            composeRule.onNodeWithText("Exact requested tool action").assertExists()
            composeRule.onNodeWithText("Review the tool name and arguments below before deciding.").assertExists()
            composeRule.onNodeWithTag("approval-detail-list")
                .performScrollToNode(hasText("{\"command\":\"echo exact\"}", substring = true))
            composeRule.onNodeWithText("{\"command\":\"echo exact\"}", substring = true)
                .assertIsDisplayed()
            composeRule.onNodeWithTag("approval-file-changes").assertDoesNotExist()
            composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
            composeRule.onNodeWithText("Reject").assertIsEnabled()
            composeRule.onNodeWithText("Review the exact tool name and arguments above, then acknowledge before approving.")
                .assertExists()
            composeRule.onNodeWithTag("approval-detail-list")
                .performScrollToNode(hasTestTag("approval-exact-tool-review-acknowledgement"))
            composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
                .performClick()
            composeRule.onNodeWithText("Approve request").assertIsEnabled()
        }
    }

    @Test
    fun exactToolRequestPresentsProviderAndDecisionContextBeforeArguments() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            sourceProvider = "CODEX",
            projectName = "Payments service",
            verifiedSource = true,
            sourceInvocationId = "codex-session-7:tool-3",
            files = listOf(
                FileChangeSummary(
                    path = "codex://tool/shell",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo exact\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        composeRule.setContent {
            Box(modifier = Modifier.width(400.dp).height(1200.dp)) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }

        composeRule.onNodeWithTag("approval-detail-hero").assertExists()
        composeRule.onNodeWithTag("approval-exact-tool-decision-context").assertExists()
        composeRule.onNodeWithText("CODEX · Verified source").assertExists()
        composeRule.onNodeWithText("Payments service").assertExists()
        composeRule.onNodeWithText("Exact requested tool action").assertExists()

        val heroBottom = composeRule.onNodeWithTag("approval-detail-hero")
            .fetchSemanticsNode().boundsInRoot.bottom
        val contextBounds = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
            .fetchSemanticsNode().boundsInRoot
        val actionHeaderTop = composeRule.onNodeWithTag("approval-exact-tool-request-header")
            .fetchSemanticsNode().boundsInRoot.top
        assertTrue("Decision context must follow the request header", heroBottom <= contextBounds.top)
        assertTrue("Exact arguments must follow the decision context", contextBounds.bottom <= actionHeaderTop)
    }

    @Test
    fun exactToolReviewAcknowledgementResetsWhenPayloadChanges() {
        val original = approval(ApprovalStatus.REQUESTED).copy(
            files = listOf(
                FileChangeSummary(
                    path = "codex://tool/shell",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo first\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        val currentApproval = androidx.compose.runtime.mutableStateOf(original)
        composeRule.setContent {
            ApprovalDetailContent(currentApproval.value, null, {}, {}, {})
        }

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasTestTag("approval-exact-tool-review-acknowledgement"))
        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(
                files = original.files.map { it.copy(diff = "{\"command\":\"echo changed\"}") }
            )
        }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun exactToolReviewAcknowledgementResetsWhenInvocationChanges() {
        val original = approval(ApprovalStatus.REQUESTED).copy(
            files = listOf(
                FileChangeSummary(
                    path = "codex://tool/shell",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo unchanged\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        val currentApproval = androidx.compose.runtime.mutableStateOf(original)
        composeRule.setContent {
            ApprovalDetailContent(currentApproval.value, null, {}, {}, {})
        }

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasTestTag("approval-exact-tool-review-acknowledgement"))
        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(sourceInvocationId = "different-invocation")
        }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun exactToolReviewAcknowledgementResetsWhenRevisionRequestReopens() {
        val original = approval(ApprovalStatus.REQUESTED).copy(
            files = listOf(
                FileChangeSummary(
                    path = "codex://tool/shell",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo unchanged\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        val currentApproval = androidx.compose.runtime.mutableStateOf(original)
        composeRule.setContent {
            ApprovalDetailContent(currentApproval.value, null, {}, {}, {})
        }

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasTestTag("approval-exact-tool-review-acknowledgement"))
        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(status = ApprovalStatus.REVISION_REQUESTED)
        }
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()

        composeRule.runOnIdle { currentApproval.value = original }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun highRiskFileApprovalRequiresOpeningAndAcknowledgingFullDiff() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            risk = RiskLevel.HIGH,
            files = listOf(
                FileChangeSummary("src/payment.kt", "update", "- old\n+ new", RiskLevel.HIGH)
            )
        )
        var fullDiffOpenCount = 0
        composeRule.setContent {
            ApprovalDetailContent(
                approval = request,
                lastActionMessage = null,
                onOpenDiff = { fullDiffOpenCount += 1 },
                onApprove = {},
                onReject = {}
            )
        }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasTestTag("approval-full-diff-review-acknowledgement"))
        composeRule.onNodeWithTag("approval-full-diff-review-acknowledgement")
            .assertIsNotEnabled()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasTestTag("approval-full-diff-review"))
        composeRule.onNodeWithTag("approval-full-diff-review").performClick()
        composeRule.runOnIdle { assertEquals(1, fullDiffOpenCount) }
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasTestTag("approval-full-diff-review-acknowledgement"))
        composeRule.onNodeWithTag("approval-full-diff-review-acknowledgement").performClick()

        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()
    }

    @Test
    fun fullDiffReviewAcknowledgementResetsWhenPayloadRiskOrStatusChanges() {
        val original = approval(ApprovalStatus.REQUESTED).copy(
            risk = RiskLevel.HIGH,
            files = listOf(FileChangeSummary("src/payment.kt", "update", "+first", RiskLevel.HIGH))
        )
        val currentApproval = androidx.compose.runtime.mutableStateOf(original)
        composeRule.setContent {
            ApprovalDetailContent(currentApproval.value, null, {}, {}, {})
        }

        fun openAndAcknowledge() {
            scrollDetailToTag("approval-full-diff-review")
            composeRule.onNodeWithTag("approval-full-diff-review").performClick()
            scrollDetailToTag("approval-full-diff-review-acknowledgement")
            composeRule.onNodeWithTag("approval-full-diff-review-acknowledgement").performClick()
        }

        openAndAcknowledge()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(
                files = original.files.map { it.copy(diff = "+changed payload") }
            )
        }
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        openAndAcknowledge()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle { currentApproval.value = currentApproval.value.copy(risk = RiskLevel.CRITICAL) }
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        openAndAcknowledge()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = currentApproval.value.copy(status = ApprovalStatus.REVISION_REQUESTED)
        }
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.runOnIdle { currentApproval.value = currentApproval.value.copy(status = ApprovalStatus.REQUESTED) }
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Reject").assertIsEnabled()

        openAndAcknowledge()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.runOnIdle {
            currentApproval.value = currentApproval.value.copy(id = "approval-next-invocation")
        }
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
    }

    @Test
    fun highRiskToolConfirmationShowsExactToolAndArguments() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            title = "Codex permission: shell",
            sourceProvider = "CODEX",
            risk = RiskLevel.HIGH,
            files = listOf(
                FileChangeSummary(
                    path = "codex://tool/shell",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo exact confirmation\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        scrollDetailToTag("approval-exact-tool-review-acknowledgement")
        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement").performClick()
        composeRule.onNodeWithText("Approve request").performClick()

        composeRule.onNodeWithText("Confirm high-risk approval").assertExists()
        composeRule.onNodeWithTag("approval-high-risk-confirmation").assertExists()
        composeRule.onNodeWithTag("approval-confirmation-tool-path")
            .assertTextContains("codex://tool/shell")
        composeRule.onNodeWithTag("approval-confirmation-tool-arguments")
            .assertTextContains("{\"command\":\"echo exact confirmation\"}")
        composeRule.onNodeWithText("Approve this request").assertIsEnabled()
    }

    @Test
    fun ordinaryFileApprovalRetainsFileChangesSection() {
        val request = approval(ApprovalStatus.REQUESTED)
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        composeRule.onNodeWithTag("approval-exact-tool-request").assertDoesNotExist()
        scrollDetailToTag("approval-file-changes")
        composeRule.onNodeWithTag("approval-file-changes").assertExists()
        composeRule.onNodeWithText("File Changes").assertExists()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("+test"))
        composeRule.onNodeWithText("+test").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
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
    fun compactHeightKeepsDecisionActionsReachableWithoutTruncation() {
        composeRule.setContent {
            Box(modifier = Modifier.width(360.dp).height(240.dp).testTag("compact-approval-viewport")) {
                ApprovalDetailContent(approval(ApprovalStatus.REQUESTED), null, {}, {}, {})
            }
        }

        val viewport = composeRule.onNodeWithTag("compact-approval-viewport").fetchSemanticsNode().boundsInRoot
        val reject = composeRule.onNodeWithText("Reject").performScrollTo().assertIsDisplayed()
        val rejectBounds = reject.fetchSemanticsNode().boundsInRoot
        assertTrue("Reject must remain inside the compact-height viewport", rejectBounds.top >= viewport.top && rejectBounds.bottom <= viewport.bottom)
        assertTrue("Reject action should remain at least 48dp tall", rejectBounds.height >= 48f)

        val approve = composeRule.onNodeWithText("Approve request").performScrollTo().assertIsDisplayed()
        val approveBounds = approve.fetchSemanticsNode().boundsInRoot
        assertTrue("Approve must remain inside the compact-height viewport", approveBounds.top >= viewport.top && approveBounds.bottom <= viewport.bottom)
        assertTrue("Approve action should remain at least 48dp tall", approveBounds.height >= 48f)
        assertTrue(
            "Compact-height decision actions should both span the available width",
            kotlin.math.abs(rejectBounds.width - approveBounds.width) < 1f
        )
        composeRule.onNodeWithTag("approval-decision-context")
            .assertTextContains("ANTIGRAVITY")
            .assertTextContains("Approval detail test")
            .assertTextContains("Risk: Low")
        composeRule.onNodeWithTag("approval-decision-status").performScrollTo()
        composeRule.onNodeWithTag("approval-decision-status")
            .assertTextContains("Confirm only after reviewing diff, tests, and rollback.")
    }

    @Test
    fun decisionActionAccessibilityLabelsIdentifyRequestAndRisk() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.REQUESTED).copy(
                    title = "Delete production database",
                    risk = RiskLevel.CRITICAL
                ),
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = {}
            )
        }

        assertEquals(
            listOf("Reject Delete production database. Risk level: Critical."),
            composeRule.onNodeWithText("Reject").fetchSemanticsNode().config[ContentDescription]
        )
        assertEquals(
            listOf("Approve Delete production database. Risk level: Critical."),
            composeRule.onNodeWithText("Approve request").fetchSemanticsNode().config[ContentDescription]
        )
    }

    @Test
    fun compactHeightAndLargeFontKeepExactActionAndDecisionControlsReachable() {
        assertExactToolRequestReviewableAtLargeFontScale(
            viewportWidth = 360.dp,
            viewportHeight = 240.dp
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
        status.assertTextContains("Approval is available.", substring = true)
        assertEquals(
            LiveRegionMode.Polite,
            status.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )

        composeRule.runOnIdle { decisionInFlight.value = true }

        status.assertTextContains("Sending the decision and waiting for the server.", substring = true)
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

        val statusBounds = composeRule.onNodeWithText("Review required").fetchSemanticsNode().boundsInRoot
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
                        approval(ApprovalStatus.REQUESTED).copy(projectName = "Production workspace"),
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
    fun exactToolRequestRemainsReviewableAt200PercentFontScaleAndNarrowWidth() {
        assertExactToolRequestReviewableAtLargeFontScale(
            viewportWidth = 260.dp,
            viewportHeight = 640.dp
        )
    }

    @Test
    fun exactToolRequestAndDecisionControlsRemainReachableAt200PercentFontScaleAndCompactHeight() {
        assertExactToolRequestReviewableAtLargeFontScale(
            viewportWidth = 320.dp,
            viewportHeight = 240.dp
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
            expiresAt = Instant.now().plusSeconds(600).toString(),
            verifiedSource = true,
            riskReasons = listOf("Antigravity command can cause broad side effects.")
        )
        composeRule.setContent {
            ApprovalDetailContent(request, null, {}, {}, {})
        }

        composeRule.onNodeWithText("Risk rationale").assertExists()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("ANTIGRAVITY · Verified source"))
        composeRule.onNodeWithText("ANTIGRAVITY · Verified source").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasText("main"))
        composeRule.onNodeWithText("main").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasText("Show technical details"))
        composeRule.onNodeWithText("Show technical details").assertIsDisplayed()
        composeRule.onAllNodesWithText("Approval ID: approval-detail-test", substring = true).assertCountEquals(0)
        composeRule.swipeApprovalListUntilDisplayed("antigravity:session-codex-42:tool-19")
        composeRule.onNodeWithText("antigravity:session-codex-42:tool-19").assertExists()
        composeRule.onNodeWithText("Show technical details").performClick()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Approval ID: approval-detail-test", substring = true))
        composeRule.onNodeWithText("Approval ID: approval-detail-test", substring = true).assertExists()
        composeRule.onNodeWithText("Project ID: project-production", substring = true).assertExists()
        composeRule.onNodeWithText("Source agent ID: agent-antigravity-01", substring = true).assertExists()
        composeRule.onNodeWithText("Session ID: session-codex-42", substring = true).assertExists()
        composeRule.onNodeWithText("Source invocation ID: antigravity:session-codex-42:tool-19", substring = true).assertExists()
        composeRule.onNodeWithText(
            "Expires at: ${formatLocalizedApprovalExpiry(request.expiresAt)}",
            substring = true
        ).assertExists()
        composeRule.onNodeWithText("Source verified: Yes", substring = true).assertExists()
        composeRule.onNodeWithText(
            "Antigravity command can cause broad side effects.",
            substring = true
        ).assertExists()
    }

    @Test
    fun approvalDecisionContextIsReachableFromSummaryBeforeDecision() {
        val longInvocationId = "antigravity:" + "session-42:tool-19/".repeat(12)
        val request = approval(ApprovalStatus.REQUESTED).copy(
            projectName = "Payments service",
            targetBranch = "release/production",
            sourceProvider = "ANTIGRAVITY",
            sourceInvocationId = longInvocationId,
            verifiedSource = true,
            expiresAt = Instant.now().plusSeconds(600).toString(),
            riskReasons = listOf(
                "Command may modify production configuration.",
                "Command runs with workspace write access."
            )
        )
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        composeRule.onNodeWithText("Decision context").assertExists()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Command may modify production configuration.", substring = true))
        composeRule.onNodeWithText("Command may modify production configuration.", substring = true)
            .assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasText("Payments service"))
        composeRule.onNodeWithText("Payments service").assertIsDisplayed()
        composeRule.onNodeWithText("release/production").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Command runs with workspace write access.", substring = true))
        composeRule.onNodeWithText("Command runs with workspace write access.", substring = true)
            .assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("ANTIGRAVITY · Verified source"))
        composeRule.onNodeWithText("ANTIGRAVITY · Verified source").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasText(longInvocationId))
        composeRule.swipeApprovalListUntilDisplayed(longInvocationId)
        composeRule.onNodeWithText(longInvocationId).assertIsDisplayed()
        val formattedExpiry = formatLocalizedApprovalExpiry(request.expiresAt)
        composeRule.swipeApprovalListUntilDisplayed(formattedExpiry)
        composeRule.onNodeWithText(formattedExpiry).assertIsDisplayed()
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

        composeRule.onAllNodesWithText("Not provided").assertCountEquals(4)
        composeRule.onNodeWithText("ANTIGRAVITY · Source not verified").assertExists()
        composeRule.onNodeWithText("No risk rationale was supplied.").assertExists()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasText("Invocation"))
        composeRule.onNodeWithText("Invocation").assertIsDisplayed()
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

private fun ApprovalDetailScreenTest.scrollDetailToTag(tag: String) {
    composeRule.onNodeWithTag("approval-detail-list").performScrollToNode(hasTestTag(tag))
}

private fun ApprovalDetailScreenTest.assertExactToolRequestReviewableAtLargeFontScale(
    viewportWidth: androidx.compose.ui.unit.Dp,
    viewportHeight: androidx.compose.ui.unit.Dp
) {
    val exactTitle = "Antigravity approval: run_command for production workspace deployment"
    val exactArguments = "{\"command\":\"./deploy.sh --environment production --confirm-target aixion-api\",\"cwd\":\"/workspace/control-tower\"}"
    val request = approval(ApprovalStatus.REQUESTED).copy(
        title = exactTitle,
        summary = "Run the exact command requested by the connected agent.",
        sourceProvider = "ANTIGRAVITY",
        files = listOf(
            FileChangeSummary(
                path = "antigravity://tool/run_command",
                changeType = "tool_call",
                diff = exactArguments,
                risk = RiskLevel.LOW
            )
        )
    )

    composeRule.setContent {
        CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
            Box(modifier = Modifier.width(viewportWidth).height(viewportHeight).testTag("large-text-approval-viewport")) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }
    }

    composeRule.swipeApprovalListUntilDisplayed(exactTitle)
    composeRule.onNodeWithText(exactTitle).assertIsDisplayed()
    composeRule.onNodeWithTag("approval-detail-list")
        .performScrollToNode(hasText(exactArguments, substring = true))
    composeRule.onNodeWithText(exactArguments, substring = true)
        .assertIsDisplayed()
        .assertTextContains(exactArguments, substring = true)

    // Compact layouts let the decision gate scroll without truncating either action.
    val viewportBounds = composeRule.onNodeWithTag("large-text-approval-viewport").fetchSemanticsNode().boundsInRoot
    val reject = composeRule.onNodeWithText("Reject").performScrollTo().assertIsDisplayed().assertIsEnabled()
    val rejectBounds = reject.fetchSemanticsNode().boundsInRoot
    composeRule.onNodeWithText("Approve request").performScrollTo().assertIsDisplayed().assertIsNotEnabled()
    val approveBounds = composeRule.onNodeWithText("Approve request").fetchSemanticsNode().boundsInRoot
    assertTrue("Reject target must be at least 48dp tall", rejectBounds.height >= 48f)
    assertTrue("Approve target must be at least 48dp tall", approveBounds.height >= 48f)
    assertTrue("Reject target must fit in viewport", rejectBounds.top >= viewportBounds.top && rejectBounds.bottom <= viewportBounds.bottom)
    assertTrue("Approve target must fit in viewport", approveBounds.top >= viewportBounds.top && approveBounds.bottom <= viewportBounds.bottom)

    scrollDetailToTag("approval-exact-tool-review-acknowledgement")
    composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement").performClick()
    composeRule.onNodeWithText("Approve request").assertIsEnabled()
}

private fun ApprovalDetailScreenTest.swipeApprovalListUntilDisplayed(text: String, maxSwipes: Int = 12) {
    repeat(maxSwipes) {
        val visible = runCatching {
            composeRule.onNodeWithText(text, substring = true).assertIsDisplayed()
        }.isSuccess
        if (visible) return
        composeRule.onNodeWithTag("approval-detail-list").performTouchInput { swipeUp() }
    }
    composeRule.onNodeWithText(text, substring = true).assertIsDisplayed()
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
