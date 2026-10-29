package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.Density
import androidx.compose.runtime.CompositionLocalProvider
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalExactToolContextTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun providerAndProjectAppearBeforeExactPayloadAndAcknowledgementAtCompactWidth() {
        val payload = "{\"command\":\"git status --short\"}"
        val approval = ApprovalSummary(
            id = "approval-source-context-test",
            projectName = "Mobile app workspace",
            title = "Check repository state",
            summary = "A pending Codex shell approval.",
            agentName = "Codex",
            sourceProvider = "CODEX",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = listOf(
                FileChangeSummary("codex://tool/shell", "tool_call", payload, RiskLevel.LOW)
            ),
            sourceInvocationId = "codex-run-87654321",
            expiresAt = "2099-01-02T03:04:05Z",
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = ""
        )

        composeRule.setContent {
            Box(modifier = Modifier.size(width = 320.dp, height = 480.dp)) {
                ApprovalDetailContent(
                    approval = approval,
                    lastActionMessage = null,
                    onOpenDiff = {},
                    onApprove = {},
                    onReject = {}
                )
            }
        }

        val context = composeRule.onNodeWithTag("approval-exact-tool-context")
        val header = composeRule.onNodeWithTag("approval-exact-tool-request")
        val headerTitle = composeRule.onNodeWithText("Exact requested tool action")
        val path = composeRule.onNodeWithText("codex://tool/shell")
        val exactPayload = composeRule.onNodeWithText(payload)
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")

        val requestList = composeRule.onNodeWithTag("approval-detail-list")
        requestList.performScrollToNode(hasTestTag("approval-exact-tool-request"))
        context.assertExists()
        header.assertExists()
        context.assertTextContains("Provider: Codex · Project: Mobile app workspace")
        composeRule.onNodeWithContentDescription(
            "Deny request ${shortSafeApprovalId(approval.id)} from Codex: Check repository state. Risk: low. Invocation: 87654321."
        ).assertExists()
        composeRule.onNodeWithContentDescription(
            "Approve request ${shortSafeApprovalId(approval.id)} from Codex: Check repository state. Risk: low. Invocation: 87654321."
        ).assertExists()
        assertTrue(
            "Provider and project context follows the exact action header",
            headerTitle.fetchSemanticsNode().boundsInRoot.bottom <= context.fetchSemanticsNode().boundsInRoot.top
        )

        val pathAbsoluteBounds = absoluteVerticalBounds(requestList, composeRule, hasText("codex://tool/shell"))
        path.assertIsDisplayed()
        val payloadAbsoluteBounds = absoluteVerticalBounds(requestList, composeRule, hasText(payload))
        exactPayload.assertIsDisplayed()
        composeRule.onNodeWithTag("approval-payload-horizontal-scroll-hint").assertDoesNotExist()
        assertTrue(
            "The request path precedes the exact payload in rendered content",
            pathAbsoluteBounds.second <= payloadAbsoluteBounds.first
        )

        val acknowledgementAbsoluteBounds = absoluteVerticalBounds(
            requestList,
            composeRule,
            hasTestTag("approval-exact-tool-review-acknowledgement")
        )
        acknowledgement.assertIsDisplayed()
        assertTrue(
            "The review acknowledgement follows the exact payload in rendered content",
            payloadAbsoluteBounds.second <= acknowledgementAbsoluteBounds.first
        )
    }

    @Test
    fun longUnbrokenPayloadStaysInsideScrollableSurfaceAndExposesFullTextSemantics() {
        val payload = "prefix ${"x".repeat(180)} suffix"
        val approval = ApprovalSummary(
            id = "approval-long-payload-test",
            projectName = "Mobile app workspace",
            title = "Review long tool argument",
            summary = "A pending tool approval with a long unbroken argument.",
            agentName = "Codex",
            sourceProvider = "CODEX",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = listOf(
                FileChangeSummary("codex://tool/shell", "tool_call", payload, RiskLevel.LOW)
            ),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = ""
        )

        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(modifier = Modifier.size(width = 320.dp, height = 480.dp)) {
                    ApprovalDetailContent(approval, null, {}, {}, {})
                }
            }
        }

        val requestList = composeRule.onNodeWithTag("approval-detail-list")
        requestList.performScrollToNode(hasTestTag("approval-payload-wrap-toggle"))
        val wrapToggle = composeRule.onNodeWithTag("approval-payload-wrap-toggle")
        wrapToggle.performClick()
        composeRule.waitForIdle()
        wrapToggle.assertIsOff()
        val payloadNode = composeRule.onNodeWithText(payload)
        payloadNode.assertExists()
        payloadNode.assertTextContains(payload)

        val scrollNode = composeRule.onNodeWithTag("approval-unbroken-payload-scroll-0")
        scrollNode.assertExists()
        val scrollSemantics = scrollNode.fetchSemanticsNode().config
        assertTrue(
            "Long unbroken payload should expose horizontal-scroll semantics",
            scrollSemantics.contains(SemanticsProperties.HorizontalScrollAxisRange)
        )
        assertTrue(
            "The payload scroller should have content beyond its visible viewport",
            scrollSemantics[SemanticsProperties.HorizontalScrollAxisRange].maxValue() > 0f
        )
        assertTrue(
            "Payload scrolling must stay within the compact screen width",
            scrollNode.fetchSemanticsNode().boundsInRoot.width <= 320f
        )
        composeRule.onNodeWithText("Swipe horizontally to inspect the full line.").assertExists()
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
