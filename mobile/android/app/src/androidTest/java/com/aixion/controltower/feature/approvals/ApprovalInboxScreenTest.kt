package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertAll
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalHistoryFile
import com.aixion.controltower.core.model.ApprovalHistoryRequestSnapshot
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.formatApprovalExpiry
import com.aixion.controltower.core.ui.ForegroundRefreshEffect
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalInboxScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun rendersApprovalBucketsForCanonicalAndLegacyStatuses() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvals = listOf(
                        approval(id = "approval_requested", title = "Pending approval", status = ApprovalStatus.REQUESTED),
                        approval(id = "approval_legacy", title = "Legacy pending", status = ApprovalStatus.PENDING_REVIEW),
                        approval(id = "approval_blocked", title = "Blocked approval", status = ApprovalStatus.BLOCKED),
                        approval(id = "approval_approved", title = "Approved approval", status = ApprovalStatus.APPROVED),
                    )
                )
            )
        }

        composeRule.onAllNodesWithText("Approvals").assertCountEquals(1)
        assertTrue(
            "The approvals page title should be exposed as a heading",
            composeRule.onNodeWithText("Approvals").fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onNodeWithText("Review pending requests or browse recorded decisions.").assertExists()
        composeRule.onNodeWithText("Needs review 2").assertExists()
        composeRule.onNodeWithText("Status blocked 1").assertExists()
        val inbox = composeRule.onNodeWithTag("approval-inbox-list")
        inbox.performScrollToNode(hasText("Pending approval"))
        composeRule.onNodeWithText("Pending approval").assertExists()
        inbox.performScrollToNode(hasText("Blocked approval"))
        composeRule.onNodeWithText("Blocked approval").assertExists()
        composeRule.onNodeWithText("Blocked").assertExists()
        composeRule.onAllNodesWithText("Approve").assertCountEquals(0)
        composeRule.onAllNodesWithText("Approved approval").assertCountEquals(0)
        inbox.performScrollToNode(hasText("Pending approval"))
        composeRule.onAllNodesWithText("Aixion Control Tower").assertCountEquals(3)
        composeRule.onAllNodesWithText("Regression approval summary").assertCountEquals(3)
        composeRule.onAllNodesWithText("Low").assertCountEquals(3)
        composeRule.onNodeWithText("Pending review").assertExists()
        composeRule.onAllNodesWithText("Review request").assertAll(
            SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button)
        )
    }

    @Test
    fun inboxSectionsWrapWithinNarrowLargeTextLayout() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp).testTag("narrow-inbox-viewport")) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1.5f)) {
                    ApprovalInboxContent(state = ApprovalsUiState(loading = false))
                }
            }
        }

        val reviewSection = composeRule.onNodeWithText("Needs review")
        val historySection = composeRule.onNodeWithText("History")
        val viewport = composeRule.onNodeWithTag("narrow-inbox-viewport").fetchSemanticsNode().boundsInRoot
        reviewSection.assertExists()
        historySection.assertExists()
        historySection.performClick()
        val finalHistoryFilter = composeRule.onNodeWithText("Revision requested")
        finalHistoryFilter.assertExists()
        composeRule.waitForIdle()
        assertTrue(
            "Needs review tab should remain within the narrow viewport",
            reviewSection.fetchSemanticsNode().boundsInRoot.let { it.left >= viewport.left && it.right <= viewport.right }
        )
        assertTrue(
            "History tab should remain within the narrow viewport",
            historySection.fetchSemanticsNode().boundsInRoot.let { it.left >= viewport.left && it.right <= viewport.right }
        )
        assertTrue(
            "History filters should wrap inside the narrow viewport rather than requiring a horizontal swipe",
            finalHistoryFilter.fetchSemanticsNode().boundsInRoot.let { it.left >= viewport.left && it.right <= viewport.right }
        )
    }

    @Test
    fun longApprovalTitleWrapsAtTwoHundredPercentFontScale() {
        val title = "Review the proposed changes to the production approval connector"
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp).testTag("narrow-inbox-viewport")) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    ApprovalInboxContent(
                        state = ApprovalsUiState(
                            loading = false,
                            approvals = listOf(
                                approval("large-text", title, ApprovalStatus.REQUESTED)
                            )
                        )
                    )
                }
            }
        }

        val titleNode = composeRule.onNodeWithText(title)
        titleNode.assertExists()
        composeRule.waitForIdle()
        val bounds = titleNode.fetchSemanticsNode().boundsInRoot
        val viewport = composeRule.onNodeWithTag("narrow-inbox-viewport").fetchSemanticsNode().boundsInRoot
        assertTrue("Approval title must remain inside the narrow screen", bounds.left >= viewport.left && bounds.right <= viewport.right)
        assertTrue("Approval title must wrap instead of clipping", bounds.height > 48f)
    }

    @Test
    fun readOnlyInboxCardsDoNotShowReviewAffordance() {
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    refreshing = true,
                    approvals = listOf(
                        approval(id = "approval_refreshing", title = "Refreshing approval", status = ApprovalStatus.REQUESTED)
                    )
                )
            )
        }

        composeRule.onNodeWithText("Refreshing approval").assertExists()
        composeRule.onAllNodesWithText("Review request").assertCountEquals(0)
    }

    @Test
    fun approvalCardsShowLocalExpiryOrExplicitUnavailableState() {
        val validExpiry = Instant.now().plusSeconds(600).toString()
        val approvals = listOf(
            approval("approval_valid_expiry", "Valid expiry", ApprovalStatus.REQUESTED)
                .copy(expiresAt = validExpiry),
            approval("approval_missing_expiry", "Missing expiry", ApprovalStatus.REQUESTED),
            approval("approval_invalid_expiry", "Invalid expiry", ApprovalStatus.REQUESTED)
                .copy(expiresAt = "not-a-timestamp")
        )
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false, approvals = approvals)
            )
        }

        val validLabel = formatApprovalExpiry(validExpiry)
        composeRule.onNodeWithText(validLabel).assertExists()
        composeRule.onAllNodesWithText("Expiry unavailable").assertCountEquals(2)
    }

    @Test
    fun clickingApprovalCardReturnsSelectedApproval() {
        var selectedId: String? = null

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvals = listOf(
                        approval(id = "approval_a", title = "First approval", status = ApprovalStatus.REQUESTED),
                        approval(id = "approval_b", title = "Second approval", status = ApprovalStatus.REQUESTED),
                    )
                ),
                onApprovalSelected = { selectedId = it.id }
            )
        }

        composeRule.onNodeWithTag("approval-inbox-list")
            .performScrollToNode(hasText("Second approval"))
        composeRule.onNodeWithText("Second approval").performClick()

        assertEquals("approval_b", selectedId)
    }

    @Test
    fun rendersLoadingCopyWhileApprovalsLoad() {
        composeRule.setContent {
            ApprovalInboxContent(state = ApprovalsUiState(loading = true))
        }

        composeRule.onNodeWithText("Loading requests…").assertExists()
        composeRule.onNodeWithText("Needs review 0").assertExists()
    }

    @Test
    fun emptyInboxCanRefreshAndInvokesReload() {
        var refreshRequested = false
        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false),
                onRetry = { refreshRequested = true }
            )
        }

        composeRule.onNodeWithText("Refresh").performClick()

        assertEquals(true, refreshRequested)
    }

    @Test
    fun unknownRiskRemainsVisibleWithDecisionsDisabled() {
        val unrecognized = approval(
            id = "approval_unknown_risk",
            title = "Unrecognized risk request",
            status = ApprovalStatus.REQUESTED
        ).copy(risk = RiskLevel.UNKNOWN)

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(loading = false, approvals = listOf(unrecognized))
            )
        }

        composeRule.onNodeWithText("Unsupported data · decisions disabled").assertExists()
        composeRule.onNodeWithText("Unrecognized risk request").assertExists()
    }

    @Test
    fun foregroundRefreshRunsOnlyWhileScreenIsStarted() {
        var refreshCount = 0
        val lifecycleOwner = object : LifecycleOwner {
            override val lifecycle = LifecycleRegistry(this)
        }
        composeRule.setContent {
            CompositionLocalProvider(LocalLifecycleOwner provides lifecycleOwner) {
                ForegroundRefreshEffect(onRefresh = { refreshCount++; true }, intervalMillis = 25)
            }
        }

        composeRule.runOnIdle { lifecycleOwner.lifecycle.currentState = Lifecycle.State.CREATED }
        composeRule.runOnIdle { lifecycleOwner.lifecycle.currentState = Lifecycle.State.STARTED }
        composeRule.waitUntil(timeoutMillis = 2_000) { refreshCount >= 2 }
        composeRule.runOnIdle { lifecycleOwner.lifecycle.currentState = Lifecycle.State.CREATED }
        // Recompose with a stopped screen and ensure the cancelled loop stays quiet.
        composeRule.waitForIdle()
        var countAfterStop = 0
        composeRule.runOnIdle { countAfterStop = refreshCount }
        Thread.sleep(100)
        composeRule.runOnIdle { assertEquals(countAfterStop, refreshCount) }
    }

    @Test
    fun historyUsesImmutableDecisionAndVerifiedSnapshotInsteadOfCurrentApprovalStatus() {
        val currentApproval = approval(
            id = "approval-immutable-history",
            title = "Mutable record currently says approved",
            status = ApprovalStatus.APPROVED
        )
        val event = historyEvent(
            decision = "reject",
            reviewerLabel = "You",
            requestSnapshotVerified = true,
            requestSnapshot = historySnapshot(title = "Original request that was denied")
        )

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvals = listOf(currentApproval),
                    approvalHistory = listOf(event),
                    historyLoaded = true
                )
            )
        }

        composeRule.onNodeWithText("History").performClick()

        composeRule.onNodeWithText("Denied").assertExists()
        composeRule.onNodeWithText("Original request that was denied").assertExists()
        composeRule.onNodeWithText("Reviewer · You").assertExists()
        composeRule.onNodeWithText(
            "Decision record only. Delivery to the agent and execution are not confirmed here."
        ).assertExists()
        composeRule.onAllNodesWithText("Mutable record currently says approved").assertCountEquals(0)
        composeRule.onAllNodesWithText("Execution complete").assertCountEquals(0)
    }

    @Test
    fun historyHidesUnverifiedSnapshotAndShowsGenericUnavailableState() {
        val event = historyEvent(
            decision = "approve",
            reviewerLabel = "Workspace reviewer",
            requestSnapshotVerified = false,
            requestSnapshot = historySnapshot(title = "Unverified secret request")
        )

        composeRule.setContent {
            ApprovalInboxContent(
                state = ApprovalsUiState(
                    loading = false,
                    approvalHistory = listOf(event),
                    historyLoaded = true
                )
            )
        }

        composeRule.onNodeWithText("History").performClick()

        composeRule.onAllNodesWithText("Approved").assertCountEquals(2)
        composeRule.onNodeWithText("Reviewer · Workspace reviewer").assertExists()
        composeRule.onNodeWithText("Request details unavailable").assertExists()
        composeRule.onNodeWithText("This older or unverifiable record is shown without request details.").assertExists()
        composeRule.onAllNodesWithText("Unverified secret request").assertCountEquals(0)
        composeRule.onAllNodesWithText("Delivered to agent").assertCountEquals(0)
    }
}

private fun historyEvent(
    decision: String,
    reviewerLabel: String,
    requestSnapshotVerified: Boolean,
    requestSnapshot: ApprovalHistoryRequestSnapshot?
) = ApprovalHistoryEvent(
    eventId = "audit-event-test-only",
    approvalId = "approval-id-test-only",
    decision = decision,
    reason = "Checked the requested changes.",
    decidedAt = "2026-10-22T10:00:00Z",
    reviewerLabel = reviewerLabel,
    requestSnapshotVerified = requestSnapshotVerified,
    requestSnapshot = requestSnapshot
)

private fun historySnapshot(title: String) = ApprovalHistoryRequestSnapshot(
    projectId = "private-project-id",
    title = title,
    summary = "A request summary preserved with the decision.",
    agentName = "Antigravity",
    targetBranch = "main",
    files = listOf(ApprovalHistoryFile("src/Main.kt", "modify")),
    testPlan = listOf("Run focused tests"),
    rollbackPlan = "Revert the change",
    sourceProvider = "ANTIGRAVITY",
    sourceAgentName = "Antigravity on Mac",
    sourceSessionId = "private-session-id",
    sourceInvocationId = "private-invocation-id",
    sourceTaskUrl = "https://example.invalid/task",
    riskLevel = RiskLevel.HIGH,
    riskReasons = listOf("Review carefully"),
    requiredActions = listOf("Review")
)

private fun approval(id: String, title: String, status: ApprovalStatus): ApprovalSummary {
    return ApprovalSummary(
        id = id,
        projectName = "Aixion Control Tower",
        title = title,
        summary = "Regression approval summary",
        agentName = "codex-agent",
        targetBranch = "feature/regression",
        status = status,
        risk = RiskLevel.LOW,
        files = listOf(
            FileChangeSummary(
                path = "docs/regression.md",
                changeType = "create",
                diff = "+ regression",
                risk = RiskLevel.LOW
            )
        ),
        requiredActions = listOf("Review files"),
        testPlan = listOf("python -m pytest"),
        rollbackPlan = "Close PR"
    )
}
