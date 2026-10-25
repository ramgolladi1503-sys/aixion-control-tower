package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.formatApprovalExpiry
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalExactToolDecisionContextTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun exactToolPayloadAndDecisionContextPrecedeAcknowledgementAndDecision() {
        val expiresAt = "2099-01-02T03:04:05Z"
        val longArgument = "x".repeat(180)
        val payload = "{\"command\":\"run --token $longArgument\"}"
        val approval = ApprovalSummary(
            id = "approval-context-order-test",
            projectName = "Workspace",
            title = "Clean generated files",
            summary = "A pending tool approval.",
            agentName = "Antigravity",
            sourceProvider = "ANTIGRAVITY",
            targetBranch = "release/1.0",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = listOf(FileChangeSummary("antigravity://tool/run_command", "tool_call", payload, RiskLevel.LOW)),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "",
            sourceInvocationId = "invocation-12345678",
            expiresAt = expiresAt,
            riskReasons = listOf("Deletes generated build output")
        )

        composeRule.setContent {
            Box(Modifier.size(width = 420.dp, height = 2400.dp)) {
                ApprovalDetailContent(approval, null, {}, {}, {})
            }
        }

        val payloadNode = composeRule.onNodeWithText(payload)
        val actionHeading = composeRule.onNodeWithText("Exact requested tool action")
        val wrapToggle = composeRule.onNodeWithTag("approval-payload-wrap-toggle")
        val context = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
        val approve = composeRule.onNodeWithText("Approve request")

        context.assertExists()
        assertTrue(
            "The exact action section should be available through screen-reader heading navigation",
            actionHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        payloadNode.assertExists()
        wrapToggle.assertIsOn()
        composeRule.onNodeWithTag("approval-unbroken-payload-wrapped-0").assertExists()
        composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0").assertDoesNotExist()
        composeRule.onNodeWithTag("approval-payload-horizontal-scroll-hint").assertDoesNotExist()
        assertTrue(
            "Wrapped exact payload should occupy multiple lines for reading without horizontal scrolling",
            payloadNode.fetchSemanticsNode().boundsInRoot.height > 42f
        )
        wrapToggle.performClick()
        wrapToggle.assertIsOff()
        composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0").assertExists()
        composeRule.onNodeWithTag("approval-payload-horizontal-scroll-hint").assertExists()
        wrapToggle.performClick()
        wrapToggle.assertIsOn()
        composeRule.onNodeWithTag("approval-unbroken-payload-wrapped-0").assertExists()
        composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0").assertDoesNotExist()
        composeRule.onNodeWithText("Before you decide").assertExists()
        composeRule.onNodeWithText("release/1.0").assertExists()
        composeRule.onNodeWithText("Low").assertExists()
        composeRule.onNodeWithText("Deletes generated build output").assertExists()
        composeRule.onNodeWithText(formatApprovalExpiry(expiresAt)).assertExists()
        composeRule.onNodeWithText("12345678").assertExists()
        acknowledgement.assertExists()
        approve.assertIsNotEnabled()

        val payloadBottom = payloadNode.fetchSemanticsNode().boundsInRoot.bottom
        val contextTop = context.fetchSemanticsNode().boundsInRoot.top
        val contextBottom = context.fetchSemanticsNode().boundsInRoot.bottom
        val acknowledgementTop = acknowledgement.fetchSemanticsNode().boundsInRoot.top
        assertTrue("Exact payload must precede reviewer context", payloadBottom <= contextTop)
        assertTrue("Reviewer context must precede acknowledgement", contextBottom <= acknowledgementTop)

        acknowledgement.performClick()
        approve.assertIsEnabled()
    }

    @Test
    fun missingRationaleAndUnknownExpiryRemainExplicitAndUnknownRiskCannotDecide() {
        val approval = ApprovalSummary(
            id = "approval-context-unknown-test",
            projectName = "Workspace",
            title = "Review tool call",
            summary = "A pending tool approval.",
            agentName = "Antigravity",
            sourceProvider = "ANTIGRAVITY",
            targetBranch = "",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.UNKNOWN,
            files = listOf(FileChangeSummary("antigravity://tool/run_command", "tool_call", "{}", RiskLevel.UNKNOWN)),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = ""
        )

        composeRule.setContent {
            Box(Modifier.size(width = 420.dp, height = 2400.dp)) {
                ApprovalDetailContent(approval, null, {}, {}, {})
            }
        }

        val context = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        context.assertExists()
        composeRule.onNodeWithText("Not provided").assertExists()
        composeRule.onNodeWithText("No risk rationale was supplied by the backend.").assertExists()
        composeRule.onNodeWithText("Expiry unavailable").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
    }
}
