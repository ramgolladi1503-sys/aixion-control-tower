package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
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

        composeRule.onNodeWithText("Approval Inbox").assertExists()
        composeRule.onNodeWithText("Review agent requests before code moves.").assertExists()
        composeRule.onNodeWithText("Pending 2").assertExists()
        composeRule.onNodeWithText("Blocked 1").assertExists()
        composeRule.onNodeWithText("Approved 1").assertExists()
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

        composeRule.onNodeWithText("Loading approvals...").assertExists()
        composeRule.onNodeWithText("Pending 0").assertExists()
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
