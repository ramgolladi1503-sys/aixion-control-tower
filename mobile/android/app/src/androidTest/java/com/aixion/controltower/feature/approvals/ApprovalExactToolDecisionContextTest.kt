package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.state.ToggleableState
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performClick
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
        val executableContent = "{\"path\":\"src/unsafe.txt\",\"content\":\"different executed payload\"}"
        val approval = ApprovalSummary(
            id = "approval-context-order-test",
            projectName = "Workspace",
            title = "Clean generated files",
            summary = "A pending tool approval.",
            agentName = "Antigravity",
            sourceProvider = "ANTIGRAVITY",
            sourceAgentId = "agent-context-test",
            sourceSessionId = "session-context-test",
            targetBranch = "release/1.0",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = listOf(
                FileChangeSummary(
                    "antigravity://tool/run_command",
                    "tool_call",
                    payload,
                    RiskLevel.LOW,
                    newContent = executableContent
                )
            ),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "",
            sourceInvocationId = "invocation-12345678",
            verifiedSource = true,
            expiresAt = expiresAt,
            riskReasons = listOf("Deletes generated build output")
        )

        composeRule.setContent {
            Box(Modifier.fillMaxSize()) {
                ApprovalDetailContent(approval, null, {}, {}, {})
            }
        }

        val requestList = composeRule.onNodeWithTag("approval-detail-list")
        val payloadNode = composeRule.onNodeWithText(payload)
        val executableContentNode = composeRule.onNodeWithText(executableContent)
        val actionHeading = composeRule.onNodeWithText("Exact requested tool action")
        val wrapToggle = composeRule.onNodeWithTag("approval-payload-wrap-toggle")
        val context = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")
        val approve = composeRule.onNodeWithContentDescription(
            "Approve request ${shortSafeApprovalId(approval.id)} from Antigravity: Clean generated files. " +
                "Risk: low. Invocation: 12345678."
        )

        requestList.performScrollToNode(hasText("Exact requested tool action"))
        actionHeading.assertIsDisplayed()
        assertTrue(
            "The exact action section should be available through screen-reader heading navigation",
            actionHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        val payloadAbsoluteBounds = absoluteVerticalBounds(requestList, composeRule, hasText(payload))
        payloadNode.assertIsDisplayed()
        composeRule.onNodeWithText("Exact executable arguments").assertExists()
        wrapToggle.assertIsOn()
        composeRule.onNodeWithTag("approval-unbroken-payload-wrapped-0").assertExists()
        composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0").assertDoesNotExist()
        composeRule.onNodeWithTag("approval-payload-horizontal-scroll-hint").assertDoesNotExist()
        assertTrue(
            "Wrapped exact payload should occupy multiple lines for reading without horizontal scrolling",
            payloadNode.fetchSemanticsNode().boundsInRoot.height > 42f
        )
        requestList.performScrollToNode(hasTestTag("approval-payload-wrap-toggle"))
        wrapToggle.performClick()
        composeRule.waitUntil(timeoutMillis = 1_000) {
            wrapToggle.fetchSemanticsNode().config[SemanticsProperties.ToggleableState] == ToggleableState.Off
        }
        wrapToggle.assertIsOff()
        composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0").assertExists()
        composeRule.onNodeWithTag("approval-payload-horizontal-scroll-hint").assertExists()
        wrapToggle.performClick()
        composeRule.waitUntil(timeoutMillis = 1_000) {
            wrapToggle.fetchSemanticsNode().config[SemanticsProperties.ToggleableState] == ToggleableState.On
        }
        wrapToggle.assertIsOn()
        composeRule.onNodeWithTag("approval-unbroken-payload-wrapped-0").assertExists()
        composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0").assertDoesNotExist()
        approve.assertIsNotEnabled()

        val executableAbsoluteBounds = absoluteVerticalBounds(requestList, composeRule, hasText(executableContent))
        executableContentNode.assertIsDisplayed()
        val contextAbsoluteBounds = absoluteVerticalBounds(
            requestList,
            composeRule,
            hasTestTag("approval-exact-tool-decision-context")
        )
        context.assertIsDisplayed()
        listOf(
            "release/1.0",
            "Risk evidence",
            "Deletes generated build output",
            formatApprovalExpiry(expiresAt),
            "12345678"
        ).forEach { value -> assertTextRenderedInside(composeRule, context, value) }
        requestList.performScrollToNode(hasTestTag("approval-exact-tool-decision-context"))
        context.assertIsDisplayed()
        acknowledgement.assertIsDisplayed()
        val contextBounds = context.fetchSemanticsNode().boundsInRoot
        val acknowledgementBounds = acknowledgement.fetchSemanticsNode().boundsInRoot
        assertTrue(
            "Displayed diff must precede executable content in rendered content",
            payloadAbsoluteBounds.second <= executableAbsoluteBounds.first
        )
        assertTrue(
            "Executable content must precede reviewer context in rendered content",
            executableAbsoluteBounds.second <= contextAbsoluteBounds.first
        )
        assertTrue(
            "Reviewer context must precede acknowledgement in rendered content",
            contextBounds.bottom <= acknowledgementBounds.top
        )

        requestList.performScrollToNode(hasTestTag("approval-exact-tool-review-acknowledgement"))
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
            Box(Modifier.fillMaxSize()) {
                ApprovalDetailContent(approval, null, {}, {}, {})
            }
        }

        val requestList = composeRule.onNodeWithTag("approval-detail-list")
        requestList.performScrollToNode(hasTestTag("approval-exact-tool-decision-context"))
        val context = composeRule.onNodeWithTag("approval-exact-tool-decision-context")
        context.assertIsDisplayed()
        composeRule.onAllNodesWithText("Not provided").assertCountEquals(3)
        composeRule.onNodeWithText("No risk rationale was supplied by the backend.").assertExists()
        composeRule.onNodeWithText("Expiry unavailable").assertExists()
        composeRule.onNodeWithText("Approve request").assertDoesNotExist()
        composeRule.onNodeWithText("Deny").assertDoesNotExist()
    }
}

private fun absoluteVerticalBounds(
    list: androidx.compose.ui.test.SemanticsNodeInteraction,
    composeRule: androidx.compose.ui.test.junit4.ComposeContentTestRule,
    target: androidx.compose.ui.test.SemanticsMatcher
): Pair<Float, Float> {
    list.performScrollToNode(target)
    val scrollOffset = list.fetchSemanticsNode()
        .config[SemanticsProperties.VerticalScrollAxisRange]
        .value()
    val bounds = composeRule.onNode(target).assertIsDisplayed().fetchSemanticsNode().boundsInRoot
    return (scrollOffset + bounds.top) to (scrollOffset + bounds.bottom)
}

private fun assertTextRenderedInside(
    composeRule: androidx.compose.ui.test.junit4.ComposeContentTestRule,
    container: androidx.compose.ui.test.SemanticsNodeInteraction,
    expectedText: String
) {
    val containerBounds = container.fetchSemanticsNode().boundsInRoot
    val matchingNodes = composeRule.onAllNodesWithText(expectedText, useUnmergedTree = true)
        .fetchSemanticsNodes()
    assertTrue(
        "Expected '$expectedText' to be rendered inside the approval decision context panel",
        matchingNodes.any { node ->
            val nodeBounds = node.boundsInRoot
            nodeBounds.left >= containerBounds.left &&
                nodeBounds.right <= containerBounds.right &&
                nodeBounds.top >= containerBounds.top &&
                nodeBounds.bottom <= containerBounds.bottom
        }
    )
}
