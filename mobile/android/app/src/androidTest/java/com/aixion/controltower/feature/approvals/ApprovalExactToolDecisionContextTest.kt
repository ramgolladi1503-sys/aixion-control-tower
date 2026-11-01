package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToIndex
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalExactToolDecisionContextTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun safetyContextAppearsAfterExactPayloadBeforeAcknowledgementAndDecision() {
        val expiry = "2099-01-01T00:00:00Z"
        val request = approval(
            risk = RiskLevel.LOW,
            targetBranch = "release",
            riskReasons = listOf("Command writes only to the requested workspace."),
            expiresAt = expiry,
            sourceInvocationId = "invocation-42"
        )

        composeRule.setContent {
            Box(Modifier.size(width = 360.dp, height = 1200.dp)) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }

        composeRule.onNodeWithTag("approval-detail-list")
            // LazyColumn items: hero, decision context, tool header, exact payload, acknowledgement.
            .performScrollToIndex(3)
        val payload = composeRule.onNodeWithText("echo exact-action", substring = true)
        val context = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
        payload.assertExists()
        composeRule.onNodeWithTag("approval-detail-list").performScrollToIndex(1)
        context.assertExists()
        composeRule.onAllNodesWithText("Low").assertCountEquals(2)
        composeRule.onNodeWithText("release").assertExists()
        composeRule.onNodeWithText("Command writes only to the requested workspace.").assertExists()
        composeRule.onNodeWithText(formatLocalizedApprovalExpiry(expiry)).assertExists()
        composeRule.onNodeWithText("invocation-42").assertExists()
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToIndex(4)
        acknowledgement.assertExists()

        composeRule.onNodeWithText("Approve request").assertIsNotEnabled()
        acknowledgement.performClick()
        composeRule.onNodeWithText("Approve request").assertIsEnabled()
    }

    @Test
    fun missingContextIsExplicitAndUnknownRiskCannotBeApproved() {
        val request = approval(
            risk = RiskLevel.UNKNOWN,
            targetBranch = "",
            riskReasons = emptyList(),
            expiresAt = null,
            sourceInvocationId = null
        )

        composeRule.setContent {
            Box(Modifier.size(width = 360.dp, height = 1200.dp)) {
                ApprovalDetailContent(request, null, {}, {}, {})
            }
        }

        val context = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        composeRule.onNodeWithTag("approval-detail-list")
            .performScrollToIndex(1)
        context.assertExists()
        composeRule.onAllNodesWithText("Unknown").assertCountEquals(2)
        composeRule.onNodeWithText("Target").assertExists()
        composeRule.onAllNodesWithText("Not provided").assertCountEquals(2)
        composeRule.onNodeWithText("No risk rationale was supplied by the backend.").assertExists()
        composeRule.onNodeWithText("Expires").assertExists()
        composeRule.onNodeWithText("Invocation", substring = true).assertDoesNotExist()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
    }

    private fun approval(
        risk: RiskLevel,
        targetBranch: String,
        riskReasons: List<String>,
        expiresAt: String?,
        sourceInvocationId: String?
    ) = ApprovalSummary(
        id = "approval-review-context",
        projectName = "Control Tower workspace",
        title = "Run requested command",
        summary = "A pending exact tool approval.",
        agentName = "Antigravity",
        sourceProvider = "ANTIGRAVITY",
        sourceAgentId = sourceInvocationId?.let { "agent-42" },
        sourceSessionId = sourceInvocationId?.let { "session-42" },
        sourceInvocationId = sourceInvocationId,
        targetBranch = targetBranch,
        status = ApprovalStatus.REQUESTED,
        risk = risk,
        files = listOf(
            FileChangeSummary(
                path = "antigravity://tool/run_command",
                changeType = "tool_call",
                diff = "{\"command\":\"echo exact-action\"}",
                risk = risk
            )
        ),
        requiredActions = emptyList(),
        testPlan = emptyList(),
        rollbackPlan = "",
        expiresAt = expiresAt,
        verifiedSource = sourceInvocationId != null,
        riskReasons = riskReasons
    )
}
