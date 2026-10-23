package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalCardPreviewTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun longSummaryIsMarkedAsPreviewAndRetainedInAccessibleText() {
        val longSummary = List(24) { index ->
            "step $index explains the requested action and its target"
        }.joinToString(" ")
        val approval = ApprovalSummary(
            id = "approval_long_summary",
            projectName = "Project",
            title = "Review long request",
            summary = longSummary,
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )

        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }

        composeRule.onNodeWithText("Request preview").assertExists()
        composeRule
            .onNodeWithTag("approval-card-summary-preview")
            .assertTextEquals(longSummary)
    }

    @Test
    fun longSummaryPreviewStaysCompactWhileFullAccessibleTextIsPreserved() {
        val longSummary = List(24) { index ->
            "step $index explains the requested action and its target"
        }.joinToString(" ")
        val otherLongSummary = List(32) { index ->
            "action $index describes the requested change in detail"
        }.joinToString(" ")
        val firstHeight = renderCardHeight(longSummary)
        val secondHeight = renderCardHeight(otherLongSummary)

        assertTrue(
            "Long approval previews should remain compact and use the same visual line limit",
            kotlin.math.abs(firstHeight - secondHeight) < 1f
        )
        composeRule.onNodeWithTag("approval-card-summary-preview").assertTextEquals(otherLongSummary)
    }

    private fun renderCardHeight(summary: String): Float {
        val approval = ApprovalSummary(
            id = "approval_summary_height",
            projectName = "Project",
            title = "Review request",
            summary = summary,
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.MEDIUM,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert"
        )
        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }
        return composeRule.onNodeWithTag("approval-card").fetchSemanticsNode().boundsInRoot.height
    }

    @Test
    fun providerAndRiskArePlacedBeforeRequestSummaryAndExpiry() {
        val approval = ApprovalSummary(
            id = "approval_provider_priority",
            projectName = "Control Tower",
            title = "Review permission request",
            summary = "Run the requested command in this project.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.HIGH,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "Revert",
            sourceProvider = "ANTIGRAVITY",
            sourceAgentName = "Antigravity on Mac",
            verifiedSource = true,
            expiresAt = "2026-10-23T12:00:00Z"
        )

        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(280.dp)) {
                    ApprovalCard(approval = approval)
                }
            }
        }

        val provider = composeRule.onNodeWithText("Verified: Antigravity on Mac").fetchSemanticsNode()
        val risk = composeRule.onNodeWithText("High").fetchSemanticsNode()
        val title = composeRule.onNodeWithText("Review permission request").fetchSemanticsNode()
        val expiry = composeRule.onNodeWithText(formatApprovalExpiry(approval.expiresAt)).fetchSemanticsNode()

        assertTrue("Provider identity should appear before the request title", provider.boundsInRoot.top < title.boundsInRoot.top)
        assertTrue("Risk should appear before the request title", risk.boundsInRoot.top < title.boundsInRoot.top)
        assertTrue("Expiry should remain below the request title", expiry.boundsInRoot.top > title.boundsInRoot.top)
    }
}
