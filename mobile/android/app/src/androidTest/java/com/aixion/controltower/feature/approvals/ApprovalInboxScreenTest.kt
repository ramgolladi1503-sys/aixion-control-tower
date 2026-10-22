package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.runtime.CompositionLocalProvider
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
import org.junit.Assert.assertEquals
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

        composeRule.onAllNodesWithText("Approvals").assertCountEquals(0)
        composeRule.onNodeWithText("Review pending requests and see the decisions recorded for each one.").assertExists()
        composeRule.onNodeWithText("Needs review 2").assertExists()
        composeRule.onNodeWithText("Status blocked 1").assertExists()
        composeRule.onNodeWithText("Pending approval").assertExists()
        composeRule.onNodeWithText("Blocked approval").assertExists()
        composeRule.onNodeWithText("Approved approval").assertExists()
        composeRule.onNodeWithText("Aixion Control Tower").assertExists()
        composeRule.onNodeWithText("Regression approval summary").assertExists()
        composeRule.onNodeWithText("Low").assertExists()
        composeRule.onNodeWithText("Pending review").assertExists()
        composeRule.onAllNodesWithText("Review request").assertCountEquals(3)
        composeRule.onNodeWithText("Review request").assert(
            SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Button)
        )
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
        val validExpiry = "2026-10-22T10:00:00Z"
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
                        approval(id = "approval_b", title = "Second approval", status = ApprovalStatus.APPROVED),
                    )
                ),
                onApprovalSelected = { selectedId = it.id }
            )
        }

        composeRule.onNodeWithText("Second approval").performClick()

        assertEquals("approval_b", selectedId)
    }

    @Test
    fun rendersLoadingCopyWhileApprovalsLoad() {
        composeRule.setContent {
            ApprovalInboxContent(state = ApprovalsUiState(loading = true))
        }

        composeRule.onNodeWithText("Loading approval decisions...").assertExists()
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

        composeRule.onNodeWithText("Approved").assertExists()
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
