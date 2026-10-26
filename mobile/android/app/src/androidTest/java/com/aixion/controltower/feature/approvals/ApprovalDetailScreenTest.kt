package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performClick
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalDeliveryStatus
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.formatApprovalExpiry
import java.time.Instant
import org.junit.Rule
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ApprovalDetailScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun approvalTitleIsExposedAsAHeadingForScreenReaderNavigation() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.REQUESTED),
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = {}
            )
        }

        val title = composeRule.onNodeWithText("Approval detail test")
            .fetchSemanticsNode()
        assertTrue(
            "The approval title should be available through TalkBack heading navigation",
            title.config.contains(SemanticsProperties.Heading)
        )
    }

    @Test
    fun recordedDecisionIsShownSeparatelyFromConnectorDelivery() {
        composeRule.setContent {
            ApprovalDetailContent(
                // Workflow status can advance after a decision. The immutable submitted
                // decision must remain the label for this outcome panel.
                approval = approval(ApprovalStatus.EXECUTING),
                lastActionMessage = "Decision sent: approve.",
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                lastActionApprovalId = "approval-detail-test",
                decisionOutcomeApprovalId = "approval-detail-test",
                decisionOutcomeDecision = "approve",
                decisionOutcomeEvent = outcomeEvent(
                    decision = "approve",
                    deliveryStatus = ApprovalDeliveryStatus.CONSUMED,
                    decidedAt = "2026-10-24T10:00:00Z"
                )
            )
        }

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Decision recorded"))
        assertEquals(
            LiveRegionMode.Polite,
            composeRule.onNodeWithTag("approval-decision-outcome-panel")
                .fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
        composeRule.onNodeWithTag("approval-decision-outcome").assertIsDisplayed()
        composeRule.onNodeWithText("Server decision: Approved").assertExists()
        composeRule.onNodeWithText("Request ail-test · Event nt-12345").assertExists()
        composeRule.onNodeWithText("Recorded ${formatHistoryTime("2026-10-24T10:00:00Z")}").assertExists()
        composeRule.onNodeWithText("Decision consumed").assertExists()
        composeRule.onNodeWithText(
            "The connector claimed this decision once. This confirms protocol consumption, not agent execution or task completion."
        ).assertExists()
        composeRule.onAllNodesWithText("Agent resumed", substring = true).assertCountEquals(0)
    }

    @Test
    fun unavailableDeliveryEvidenceIsExplicitlyUnknown() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.DENIED),
                lastActionMessage = "Server confirms the reject decision was recorded.",
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                lastActionApprovalId = "approval-detail-test",
                decisionOutcomeApprovalId = "approval-detail-test",
                decisionOutcomeDecision = "reject",
                decisionOutcomeEvent = null
            )
        }

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Decision recorded"))
        composeRule.onNodeWithText("Server decision: Denied").assertExists()
        composeRule.onNodeWithText("Delivery status unknown").assertExists()
        composeRule.onNodeWithText(
            "We could not match this decision to delivery evidence. Connector receipt and agent resumption are unconfirmed."
        ).assertExists()
    }

    @Test
    fun expiryInRequestSummaryUsesReadableLocalTimeInsteadOfRawTimestamp() {
        val expiresAt = "2026-10-24T12:30:00Z"
        composeRule.setContent {
            ApprovalDetailContent(
                approval(ApprovalStatus.EXPIRED).copy(expiresAt = expiresAt),
                null,
                {},
                {},
                {}
            )
        }

        composeRule.onNodeWithText(formatApprovalExpiry(expiresAt)).assertExists()
        composeRule.onNodeWithText(expiresAt).assertDoesNotExist()
    }

    @Test
    fun expiryCountdownExposesPoliteMilestoneAnnouncementToAccessibility() {
        val now = Instant.now()
        val expiresAt = now.plusSeconds(30).toString()
        composeRule.setContent {
            ApprovalDetailContent(
                approval(ApprovalStatus.REQUESTED).copy(expiresAt = expiresAt),
                null,
                {},
                {},
                {}
            )
        }

        val countdown = composeRule.onNodeWithTag("approval-decision-expiry-countdown")
        assertEquals(
            LiveRegionMode.Polite,
            countdown.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
        assertEquals(
            listOf("Approval expires in less than one minute."),
            countdown.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
        )
    }

    @Test
    fun unknownApprovalStatusDisablesEveryDecision() {
        composeRule.setContent {
            ApprovalDetailContent(approval(ApprovalStatus.UNKNOWN), null, {}, {}, {})
        }

        composeRule.onNodeWithText("Unsupported data").assertExists()
        composeRule.onNodeWithText("Unsupported approval data").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
    }

    @Test
    fun failedInboxRefreshKeepsOpenApprovalVisibleButBlocksDecisions() {
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.REQUESTED),
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = {},
                approvalSyncError = "Backend approval sync failed. Check your connection and retry."
            )
        }

        composeRule.onNodeWithText("Latest status unavailable").assertExists()
        composeRule.onNodeWithText(
            "Latest approval status could not be confirmed. Reconnect and refresh before deciding; the request shown here may be out of date."
        ).assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
    }

    @Test
    fun expiredApprovalShowsClosedStateAndDisablesEveryDecision() {
        composeRule.setContent { ApprovalDetailContent(approval(ApprovalStatus.EXPIRED), null, {}, {}, {}) }

        composeRule.onNodeWithText("Request expired").assertExists()
        composeRule.onNodeWithText(
            "This request expired and can no longer be approved or denied."
        ).assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
    }

    @Test
    fun cancelledApprovalExplainsThatTheRequestWasCancelled() {
        composeRule.setContent { ApprovalDetailContent(approval(ApprovalStatus.CANCELLED), null, {}, {}, {}) }

        composeRule.onNodeWithText("Request cancelled").assertExists()
        composeRule.onNodeWithText(
            "This request was cancelled and can no longer be approved or denied."
        ).assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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

        composeRule.onNodeWithText("Request expired").assertExists()
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
        composeRule.onNodeWithText("Deny").assertIsEnabled()
    }

    @Test
    fun decisionControlsAnnounceRequestIdentityAndRiskToScreenReaders() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            title = "Run production migration",
            risk = RiskLevel.HIGH
        )
        composeRule.setContent {
            ApprovalDetailContent(request, null, {}, {}, {})
        }

        composeRule.onNodeWithContentDescription(
            "Deny request ail-test from Antigravity: Run production migration. Risk: high."
        ).assertExists()
        composeRule.onNodeWithContentDescription(
            "Approve request ail-test from Antigravity: Run production migration. Risk: high."
        ).assertExists()
        // Keep concise visible button labels unchanged for sighted reviewers.
        composeRule.onNodeWithText("Deny").assertExists()
        composeRule.onNodeWithText("Approve request").assertExists()
    }

    @Test
    fun denyLabelStillInvokesExistingRejectDecisionCallback() {
        var decisionSent = false
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval(ApprovalStatus.REQUESTED),
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = { decisionSent = true }
            )
        }

        composeRule.onNodeWithText("Deny").performClick()
        composeRule.runOnIdle { assertTrue(decisionSent) }
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
            composeRule.onNodeWithText("{\"command\":\"echo exact\"}")
                .assertIsDisplayed()
            composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
                .assertIsDisplayed()
            composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
            composeRule.onNodeWithTag("approval-file-changes").assertDoesNotExist()
            composeRule.onNodeWithText("Deny").assertIsEnabled()
            composeRule.onNodeWithText("Review the exact tool name and arguments above, then acknowledge before approving.")
                .assertExists()
            composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
                .performScrollTo()
                .performClick()
            composeRule.onNodeWithText("Approve request").assertIsEnabled()
        }
    }

    @Test
    fun exactToolRequestAndDecisionContextPrecedeAcknowledgement() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            files = listOf(
                FileChangeSummary(
                    path = "antigravity://tool/run_command",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo review order\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        composeRule.setContent {
            Box(modifier = Modifier.width(400.dp).height(1200.dp)) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }

        val exactHeader = composeRule.onNodeWithTag("approval-exact-tool-request")
        val exactPayload = composeRule.onNodeWithText("{\"command\":\"echo review order\"}")
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
        val overview = composeRule.onNodeWithTag("approval-detail-request-identity")
        val riskContext = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        exactHeader.assertIsDisplayed()
        exactPayload.assertIsDisplayed()
        acknowledgement.assertIsDisplayed()
        overview.assertIsDisplayed()
        riskContext.assertIsDisplayed()

        val exactHeaderTop = exactHeader.fetchSemanticsNode().boundsInRoot.top
        val payloadTop = exactPayload.fetchSemanticsNode().boundsInRoot.top
        val acknowledgementTop = acknowledgement.fetchSemanticsNode().boundsInRoot.top
        val overviewTop = overview.fetchSemanticsNode().boundsInRoot.top
        val riskContextTop = riskContext.fetchSemanticsNode().boundsInRoot.top
        assertTrue("Exact request header must precede its payload", exactHeaderTop < payloadTop)
        assertTrue("Exact payload must precede the request overview", payloadTop < overviewTop)
        assertTrue("Request overview must precede risk context", overviewTop < riskContextTop)
        assertTrue("Reviewer must reach all decision context before acknowledging", riskContextTop < acknowledgementTop)
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
    }

    @Test
    fun exactToolApprovalIncludesRequestIdentityAndRiskEvidence() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            files = listOf(
                FileChangeSummary(
                    path = "antigravity://tool/run_command",
                    changeType = "tool_call",
                    diff = "{\"command\":\"git status\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )

        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        composeRule.onNodeWithTag("approval-detail-request-identity").assertIsDisplayed()
        composeRule.onNodeWithText("Test project").assertExists()
        composeRule.onNodeWithText("Session").assertExists()
        composeRule.onNodeWithText("Invocation").assertExists()
        composeRule.onNodeWithTag("approval-exact-tool-request").assertExists()
        composeRule.onNodeWithTag("approval-exact-tool-decision-context").assertExists()
        composeRule.onNodeWithText("Risk evidence").assertExists()
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

        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performScrollTo()
            .performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(
                files = original.files.map { it.copy(diff = "{\"command\":\"echo changed\"}") }
            )
        }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Deny").assertIsEnabled()
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

        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performScrollTo()
            .performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(sourceInvocationId = "different-invocation")
        }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Deny").assertIsEnabled()
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

        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performScrollTo()
            .performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = original.copy(status = ApprovalStatus.REVISION_REQUESTED)
        }
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()

        composeRule.runOnIdle { currentApproval.value = original }

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        composeRule.onNodeWithText("Deny").assertIsEnabled()
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
        composeRule.onNodeWithText("Deny").assertIsEnabled()
        composeRule.onNodeWithText("Review full diff").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithTag("approval-full-diff-review-acknowledgement")
            .performScrollTo()
            .assertIsNotEnabled()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()

        composeRule.onNodeWithTag("approval-full-diff-review")
            .performScrollTo()
            .performClick()
        composeRule.runOnIdle { assertEquals(1, fullDiffOpenCount) }
        composeRule.onNodeWithTag("approval-full-diff-review-acknowledgement")
            .performScrollTo()
            .performClick()

        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.onNodeWithText("Deny").assertIsEnabled()
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
            composeRule.onNodeWithTag("approval-full-diff-review")
                .performScrollTo()
                .performClick()
            composeRule.onNodeWithTag("approval-full-diff-review-acknowledgement")
                .performScrollTo()
                .performClick()
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
        composeRule.onNodeWithText("Deny").assertIsEnabled()

        openAndAcknowledge()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.runOnIdle {
            currentApproval.value = currentApproval.value.copy(id = "approval-next-invocation")
        }
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
    }

    @Test
    fun exactToolReviewSurfacesUnverifiedSourceBeforeShowingTheDecisionPayload() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            sourceProvider = "ANTIGRAVITY",
            verifiedSource = false,
            files = listOf(
                FileChangeSummary(
                    "antigravity://tool/run_command",
                    "tool_call",
                    "{\"command\":\"git status --short\"}",
                    RiskLevel.LOW
                )
            )
        )
        composeRule.setContent {
            Box(modifier = Modifier.width(320.dp).height(620.dp)) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }

        val sourceTrust = composeRule.onNodeWithTag("approval-detail-source-trust")
        val exactActionPath = composeRule.onNodeWithText("antigravity://tool/run_command")
        sourceTrust.assertIsDisplayed()
        composeRule.onNodeWithText("Unverified: ANTIGRAVITY").assertIsDisplayed()
        assertTrue(
            "Source trust should be presented before the exact tool path",
            sourceTrust.fetchSemanticsNode().boundsInRoot.top <
                exactActionPath.fetchSemanticsNode().boundsInRoot.top
        )
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

        composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
            .performScrollTo()
            .performClick()
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
        composeRule.setContent {
            Box(modifier = Modifier.width(400.dp).height(1200.dp)) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }

        composeRule.onNodeWithTag("approval-exact-tool-request").assertDoesNotExist()
        val actionSection = composeRule.onNodeWithTag("approval-file-changes").assertIsDisplayed()
        val requestContext = composeRule.onNodeWithTag("approval-detail-request-context").assertIsDisplayed()
        assertTrue(
            "The requested file change should appear before secondary request metadata",
            actionSection.fetchSemanticsNode().boundsInRoot.top <
                requestContext.fetchSemanticsNode().boundsInRoot.top
        )
        composeRule.onNodeWithText("File changes").assertIsDisplayed()
        composeRule.onNodeWithText("A detail screen state test.").assertExists()
        composeRule.onNodeWithText("+test").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
    }

    @Test
    fun decisionActionsAreStackedFullWidthTargets() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                ApprovalDetailContent(approval(ApprovalStatus.REQUESTED), null, {}, {}, {})
            }
        }

        val rejectBounds = composeRule.onNodeWithText("Deny").fetchSemanticsNode().boundsInRoot
        val approveBounds = composeRule.onNodeWithText("Approve request").fetchSemanticsNode().boundsInRoot

        assertTrue("Decision buttons should be separate vertical targets", rejectBounds.bottom < approveBounds.top)
        assertTrue("Deny should remain a 48dp or taller target", rejectBounds.height >= 48f)
        assertTrue("Approve should remain a 48dp or taller target", approveBounds.height >= 48f)
        assertTrue(
            "Decision buttons should use the same full-width size",
            kotlin.math.abs(rejectBounds.width - approveBounds.width) < 1f
        )
    }

    @Test
    fun decisionActionsStayVisibleWithoutScrollingInShortLargeTextViewport() {
        val viewportHeight = 320f
        val request = approval(ApprovalStatus.REQUESTED).copy(
            title = "Review protected production rollout",
            sourceProvider = "ANTIGRAVITY",
            risk = RiskLevel.HIGH
        )
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(360.dp).height(viewportHeight.dp).testTag("approval-short-viewport")) {
                    ApprovalDetailContent(request, null, {}, {}, {})
                }
            }
        }

        composeRule.onNodeWithText("Deny").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
        val identity = composeRule.onNodeWithTag("approval-decision-identity")
        identity.assertIsDisplayed()
        assertEquals(
            listOf("Decision context: ANTIGRAVITY, request ail-test, risk high."),
            identity.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
        )
        composeRule.onNodeWithText("Request ail-test").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-decision-identity-title")
            .assertTextContains("Review request")
        val viewport = composeRule.onNodeWithTag("approval-short-viewport").fetchSemanticsNode().boundsInRoot
        val panel = composeRule.onNodeWithTag("approval-decision-panel").fetchSemanticsNode().boundsInRoot
        val deny = composeRule.onNodeWithText("Deny").fetchSemanticsNode().boundsInRoot
        val approve = composeRule.onNodeWithText("Approve request").fetchSemanticsNode().boundsInRoot

        assertTrue("Decision panel should stay inside the short viewport", panel.top >= viewport.top && panel.bottom <= viewport.bottom)
        assertTrue("Deny action should stay inside the short viewport", deny.top >= viewport.top && deny.bottom <= viewport.bottom)
        assertTrue("Approve action should stay inside the short viewport", approve.top >= viewport.top && approve.bottom <= viewport.bottom)

        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToNode(hasText("Rollback plan"))
        composeRule.onNodeWithText("Rollback plan").assertIsDisplayed()
        composeRule.onNodeWithText("Deny").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
        identity.assertIsDisplayed()
        composeRule.onNodeWithText("Request ail-test").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-decision-identity-title")
            .assertTextContains("Review request")
        assertEquals(deny.top, composeRule.onNodeWithText("Deny").fetchSemanticsNode().boundsInRoot.top)
        assertEquals(approve.top, composeRule.onNodeWithText("Approve request").fetchSemanticsNode().boundsInRoot.top)
    }

    @Test
    fun largeTextOnNormalHeightNarrowViewportUsesCompactFooterWithoutDuplicateUtterance() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            title = "Review protected production rollout",
            sourceProvider = "ANTIGRAVITY",
            risk = RiskLevel.HIGH
        )
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(360.dp).height(800.dp).testTag("approval-large-text-viewport")) {
                    ApprovalDetailContent(request, null, {}, {}, {})
                }
            }
        }

        val review = composeRule.onNodeWithTag("approval-decision-identity-title")
        review.assertIsDisplayed()
        assertEquals(
            listOf("Review request: Review protected production rollout from Antigravity, request ail-test, risk high."),
            review.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
        )
        val identity = composeRule.onNodeWithTag("approval-decision-identity")
        identity.assertIsDisplayed()
        assertEquals(
            listOf("Decision context: ANTIGRAVITY, request ail-test, risk high."),
            identity.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
        )

        val viewport = composeRule.onNodeWithTag("approval-large-text-viewport").fetchSemanticsNode().boundsInRoot
        val panel = composeRule.onNodeWithTag("approval-decision-panel").fetchSemanticsNode().boundsInRoot
        assertTrue("Large-text decision footer should remain inside the normal-height viewport", panel.bottom <= viewport.bottom)
        composeRule.onNodeWithText("Deny").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
    }

    @Test
    fun longTitleAppearsOnceAndNormalViewportDecisionActionsRemainReachable() {
        val longTitle = "Review approval for " + "production deployment configuration ".repeat(18)
        composeRule.setContent {
            Box(modifier = Modifier.width(360.dp).height(800.dp).testTag("approval-long-title-viewport")) {
                ApprovalDetailContent(
                    approval(ApprovalStatus.REQUESTED).copy(title = longTitle),
                    null,
                    {},
                    {},
                    {}
                )
            }
        }

        composeRule.onNodeWithTag("approval-decision-identity-title").assertDoesNotExist()
        composeRule.onAllNodesWithText(longTitle).assertCountEquals(1)
        composeRule.onNodeWithText(longTitle).assertIsDisplayed()
        composeRule.onNodeWithContentDescription(
            "Approve request ail-test from Antigravity: $longTitle. Risk: low. Invocation: ail-test."
        ).assertExists()
        composeRule.onNodeWithContentDescription(
            "Deny request ail-test from Antigravity: $longTitle. Risk: low. Invocation: ail-test."
        ).assertExists()
        composeRule.onNodeWithText("Deny").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()

        val viewport = composeRule.onNodeWithTag("approval-long-title-viewport").fetchSemanticsNode().boundsInRoot
        val panel = composeRule.onNodeWithTag("approval-decision-panel").fetchSemanticsNode().boundsInRoot
        assertTrue("The decision footer should stay inside the viewport", panel.bottom <= viewport.bottom)
    }

    @Test
    fun blockedCompactFooterWrapsReasonAndKeepsDisabledActionsAndIdentityVisible() {
        val request = approval(ApprovalStatus.BLOCKED).copy(
            title = "Run protected production command",
            sourceProvider = "ANTIGRAVITY",
            risk = RiskLevel.BLOCKED
        )
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(360.dp).height(420.dp).testTag("approval-blocked-compact-viewport")) {
                    ApprovalDetailContent(request, null, {}, {}, {})
                }
            }
        }

        val reason = composeRule.onNodeWithTag("approval-decision-status")
        reason.assertTextContains("This request is blocked by policy. No decision can be recorded from mobile.")
        reason.assertIsDisplayed()
        assertTrue(
            "Blocked reason must wrap instead of being ellipsized at large text scale",
            reason.fetchSemanticsNode().boundsInRoot.height >= 48f
        )
        composeRule.onNodeWithTag("approval-decision-identity").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-decision-identity-provider")
            .assertTextContains("ANTIGRAVITY").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-decision-identity-request-id")
            .assertTextContains("Request ail-test").assertIsDisplayed()
        composeRule.onNodeWithText("Deny").assertIsNotEnabled().assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled().assertIsDisplayed()
    }

    @Test
    fun expiredCompactFooterWrapsReasonAndKeepsDisabledActionsAndIdentityVisible() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            title = "Run protected production command",
            sourceProvider = "ANTIGRAVITY",
            expiresAt = "2000-01-01T00:00:00Z"
        )
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(360.dp).height(420.dp).testTag("approval-expired-compact-viewport")) {
                    ApprovalDetailContent(request, null, {}, {}, {})
                }
            }
        }

        val reason = composeRule.onNodeWithTag("approval-decision-status")
        reason.assertTextContains("This request has expired. Refresh the inbox before deciding.")
        reason.assertIsDisplayed()
        assertTrue(
            "Expired reason must wrap instead of being ellipsized at large text scale",
            reason.fetchSemanticsNode().boundsInRoot.height >= 48f
        )
        composeRule.onNodeWithTag("approval-decision-identity").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-decision-identity-provider")
            .assertTextContains("ANTIGRAVITY").assertIsDisplayed()
        composeRule.onNodeWithTag("approval-decision-identity-request-id")
            .assertTextContains("Request ail-test").assertIsDisplayed()
        composeRule.onNodeWithText("Deny").assertIsNotEnabled().assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled().assertIsDisplayed()
    }

    @Test
    fun compactExactToolPanelJumpsToTheFullCommandBeforeDecision() {
        val request = approval(ApprovalStatus.REQUESTED).copy(
            id = "approval-command-987654321",
            title = "Run the requested production shell command",
            sourceProvider = "CODEX",
            files = listOf(
                FileChangeSummary(
                    path = "codex://tool/shell",
                    changeType = "tool_call",
                    diff = "{\"command\":\"deploy --environment production\",\"payload\":\"${"x".repeat(128)}\"}",
                    risk = RiskLevel.HIGH
                )
            )
        )
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(320.dp).height(320.dp)) {
                    ApprovalDetailContent(request, null, {}, {}, {})
                }
            }
        }

        val compactTitle = composeRule.onNodeWithTag("approval-decision-identity-title")
        compactTitle.assertTextContains("Review request")
        compactTitle.assertIsDisplayed()
        composeRule.onNodeWithContentDescription(
            "Review request: Run the requested production shell command from Codex, request 87654321, risk high."
        ).assertExists()
        compactTitle.performClick()
        composeRule.onNodeWithText("Action: Run the requested production shell command").assertIsDisplayed()
        composeRule.onNodeWithText("Close").performClick()
        composeRule.onNodeWithTag("approval-decision-identity")
            .assertIsDisplayed()
        composeRule.onNodeWithText("CODEX", useUnmergedTree = true).assertIsDisplayed()
        composeRule.onNodeWithText("Request 87654321", useUnmergedTree = true).assertIsDisplayed()
        composeRule.onNodeWithText("Deny").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()

        compactTitle.performClick()

        composeRule.onNodeWithText("Action: Run the requested production shell command").assertIsDisplayed()
        composeRule.onNodeWithText("Provider: Codex · Request 87654321").assertIsDisplayed()
        composeRule.onNodeWithText("Project: Test project").assertIsDisplayed()
        composeRule.onNodeWithText("Invocation: ail-test").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Exact tool name and arguments").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("deploy --environment production", substring = true)
            .performScrollTo()
            .assertIsDisplayed()
        val payloadScroller = composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0")
        payloadScroller.performScrollTo().assertIsDisplayed()
        val scrollRange = payloadScroller.fetchSemanticsNode().config[SemanticsProperties.HorizontalScrollAxisRange]
        assertTrue("Long exact arguments must be horizontally reachable", scrollRange.maxValue() > 0f)
        val acknowledgement = composeRule.onNodeWithText("Acknowledge and continue")
        acknowledgement.assertIsNotEnabled()
        composeRule.onNodeWithTag("approval-compact-tool-review-acknowledgement")
            .performScrollTo()
            .assertIsDisplayed()
            .performClick()
        acknowledgement.assertIsEnabled()
        composeRule.onNodeWithText("Close").performClick()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()

        compactTitle.performClick()
        acknowledgement.assertIsNotEnabled()
        composeRule.onNodeWithTag("approval-compact-tool-review-acknowledgement")
            .performScrollTo()
            .assertIsDisplayed()
            .performClick()
        acknowledgement.assertIsEnabled()
        acknowledgement.performClick()

        composeRule.onNodeWithText("Approve request").assertIsEnabled()
        composeRule.onNodeWithText("Deny").assertIsDisplayed()
    }

    @Test
    fun compactToolReviewAcknowledgementResetsWhenPayloadChangesUnderSameRequestId() {
        val initial = approval(ApprovalStatus.REQUESTED).copy(
            id = "approval-same-id",
            files = listOf(
                FileChangeSummary(
                    path = "antigravity://tool/run_command",
                    changeType = "tool_call",
                    diff = "{\"command\":\"echo original\"}",
                    risk = RiskLevel.LOW
                )
            )
        )
        val currentApproval = androidx.compose.runtime.mutableStateOf(initial)
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(320.dp).height(320.dp)) {
                    ApprovalDetailContent(currentApproval.value, null, {}, {}, {})
                }
            }
        }

        composeRule.onNodeWithTag("approval-decision-identity-title").performClick()
        composeRule.onNodeWithTag("approval-compact-tool-review-acknowledgement").performClick()
        composeRule.onNodeWithText("Acknowledge and continue").assertIsEnabled()

        composeRule.runOnIdle {
            currentApproval.value = initial.copy(
                files = listOf(
                    FileChangeSummary(
                        path = "antigravity://tool/run_command",
                        changeType = "tool_call",
                        diff = "{\"command\":\"echo changed\"}",
                        risk = RiskLevel.LOW
                    )
                )
            )
        }
        composeRule.waitForIdle()

        composeRule.onNodeWithText("{\"command\":\"echo changed\"}").assertIsDisplayed()
        composeRule.onNodeWithText("Acknowledge and continue").assertIsNotEnabled()
        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
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
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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
        composeRule.onNodeWithText("Deny").assertIsEnabled()
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
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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
        composeRule.onNodeWithText("antigravity:session-codex-42:tool-19").assertDoesNotExist()
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
    fun riskRationaleRemainsVisibleAndWrapsAtLargeFontScale() {
        val rationale = "This command can modify project files, run external processes, and change configuration used by the connected agent session. Review the exact target and requested arguments before deciding."
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                ApprovalDetailContent(
                    approval(ApprovalStatus.REQUESTED).copy(riskReasons = listOf(rationale)),
                    null,
                    {},
                    {},
                    {}
                )
            }
        }

        composeRule.onNodeWithText("Risk rationale").performScrollTo().assertIsDisplayed()
        val rationaleNode = composeRule.onNodeWithText(rationale).performScrollTo()
        rationaleNode.assertIsDisplayed()
        assertTrue(
            "Risk rationale should wrap into multiple readable lines at 200% text scale",
            rationaleNode.fetchSemanticsNode().boundsInRoot.height >= 96f
        )
    }

    @Test
    fun approvalDecisionContextIsReachableFromSummaryBeforeDecision() {
        val longInvocationId = "antigravity:" + "session-42:tool-19/".repeat(12)
        val sessionId = "session-codex-42"
        val request = approval(ApprovalStatus.REQUESTED).copy(
            projectName = "Payments service",
            targetBranch = "release/production",
            sourceProvider = "ANTIGRAVITY",
            sourceSessionId = sessionId,
            sourceInvocationId = longInvocationId,
            verifiedSource = true,
            expiresAt = "2026-10-22T14:30:00Z",
            riskReasons = listOf(
                "Command may modify production configuration.",
                "Command runs with workspace write access."
            )
        )
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        composeRule.onNodeWithText("Request at a glance").assertExists()
        composeRule.onAllNodesWithText("Command may modify production configuration.", substring = true)
            .assertCountEquals(1)
        composeRule.onAllNodesWithText("Command runs with workspace write access.", substring = true)
            .assertCountEquals(1)
        composeRule.onNodeWithText("Approve request").assertIsDisplayed()
        composeRule.onNodeWithText("Deny").assertIsDisplayed()
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
        composeRule.onNodeWithText("Session").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText(shortSafeApprovalId(sessionId)).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Invocation").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText(shortSafeApprovalId(longInvocationId)).performScrollTo().assertIsDisplayed()
        composeRule.onAllNodesWithText(sessionId).assertCountEquals(0)
        composeRule.onAllNodesWithText(longInvocationId).assertCountEquals(0)
        composeRule.onNodeWithText("Show technical details").performScrollTo().performClick()
        composeRule.onNodeWithText("Session ID: $sessionId", substring = true).performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Source invocation ID: $longInvocationId", substring = true)
            .performScrollTo().assertIsDisplayed()
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

        composeRule.onAllNodesWithText("Not provided").assertCountEquals(5)
        composeRule.onNodeWithText("ANTIGRAVITY · Source not verified").assertExists()
        composeRule.onNodeWithText("No risk rationale was supplied.").assertExists()
        composeRule.onNodeWithText("Session").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Invocation").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Expires").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Show technical details").performScrollTo().performClick()
        composeRule.onNodeWithText("Source invocation ID: Not provided", substring = true).assertExists()
    }

    @Test
    fun invocationAndSessionContextStacksAtCompactWidthAndLargeFontScale() {
        val sessionId = "session-codex-42"
        val invocationId = "antigravity:session-codex-42:tool-19"
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.width(320.dp).height(620.dp)) {
                    ApprovalDetailContent(
                        approval(ApprovalStatus.REQUESTED).copy(
                            sourceSessionId = sessionId,
                            sourceInvocationId = invocationId
                        ),
                        null,
                        {},
                        {},
                        {}
                    )
                }
            }
        }

        val sessionLabel = composeRule.onNodeWithText("Session").performScrollTo()
        val sessionValue = composeRule.onNodeWithText(shortSafeApprovalId(sessionId)).performScrollTo()
        val invocationLabel = composeRule.onNodeWithText("Invocation").performScrollTo()
        val invocationValue = composeRule.onNodeWithText(shortSafeApprovalId(invocationId)).performScrollTo()

        listOf(sessionLabel, sessionValue, invocationLabel, invocationValue).forEach { it.assertIsDisplayed() }
        assertTrue(
            "Session value should stack below its label at large font scale",
            sessionValue.fetchSemanticsNode().boundsInRoot.top > sessionLabel.fetchSemanticsNode().boundsInRoot.bottom
        )
        assertTrue(
            "Invocation value should stack below its label at large font scale",
            invocationValue.fetchSemanticsNode().boundsInRoot.top > invocationLabel.fetchSemanticsNode().boundsInRoot.bottom
        )
        composeRule.onAllNodesWithText(sessionId).assertCountEquals(0)
        composeRule.onAllNodesWithText(invocationId).assertCountEquals(0)
    }

    @Test
    fun approvalDeepLinkLoadingStateDoesNotExposeDecisionActionsForAnotherApproval() {
        composeRule.setContent {
            ApprovalDetailEmptyContent(loading = true, message = null, onRetry = null)
        }

        val loadingTitle = composeRule.onNodeWithTag("approval-detail-loading").assertExists()
        assertTrue(loadingTitle.fetchSemanticsNode().config.contains(SemanticsProperties.Heading))
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
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

        val errorCopy = composeRule.onNodeWithTag("approval-detail-open-error").assertExists()
        errorCopy.assertTextContains("No other request is selected")
        composeRule.onNodeWithTag("approval-detail-technical-error").assertDoesNotExist()
        composeRule.onNodeWithText("Technical details").performClick()
        composeRule.onNodeWithTag("approval-detail-technical-error")
            .assertTextContains("not found")
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Retry approval load").performClick()
        assertEquals(1, retryCount)
    }
}

private fun outcomeEvent(
    decision: String,
    deliveryStatus: ApprovalDeliveryStatus,
    decidedAt: String
) = ApprovalHistoryEvent(
    eventId = "event-12345",
    approvalId = "approval-detail-test",
    decision = decision,
    reason = null,
    decidedAt = decidedAt,
    reviewerLabel = "You",
    deliveryStatus = deliveryStatus,
    requestSnapshotVerified = false,
    requestSnapshot = null
)

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
