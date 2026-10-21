package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.runtime.CompositionLocalProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
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

        composeRule.onNodeWithText("Approvals").assertExists()
        composeRule.onNodeWithText("Review requests from connected agents, then see every decision and its status here.").assertExists()
        composeRule.onNodeWithText("PENDING 2").assertExists()
        composeRule.onNodeWithText("BLOCKED 1").assertExists()
        composeRule.onNodeWithText("APPROVED 1").assertExists()
        composeRule.onNodeWithText("Pending approval").assertExists()
        composeRule.onNodeWithText("Blocked approval").assertExists()
        composeRule.onNodeWithText("Approved approval").assertExists()
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
        composeRule.onNodeWithText("PENDING 0").assertExists()
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

        composeRule.onNodeWithText("Refresh approvals").performClick()

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

        composeRule.onNodeWithText("UNSUPPORTED DATA · DECISIONS DISABLED").assertExists()
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
}

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
