package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
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
        val path = composeRule.onNodeWithText("codex://tool/shell")
        val exactPayload = composeRule.onNodeWithText(payload)
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")

        context.assertExists()
        header.assertExists()
        path.assertExists()
        exactPayload.assertExists()
        composeRule.onNodeWithTag("approval-payload-horizontal-scroll-hint").assertDoesNotExist()
        acknowledgement.assertExists()
        context.assertTextContains("Provider: Codex · Project: Mobile app workspace")

        val contextTop = context.fetchSemanticsNode().boundsInRoot.top
        val headerTop = header.fetchSemanticsNode().boundsInRoot.top
        val pathTop = path.fetchSemanticsNode().boundsInRoot.top
        val payloadTop = exactPayload.fetchSemanticsNode().boundsInRoot.top
        val acknowledgementTop = acknowledgement.fetchSemanticsNode().boundsInRoot.top
        assertTrue("The exact action header must precede provider and project context", headerTop < contextTop)
        assertTrue("Provider and project context must precede the invocation payload", contextTop < pathTop)
        assertTrue("The request path must precede its payload", pathTop <= payloadTop)
        assertTrue("The review acknowledgement must follow the payload", payloadTop < acknowledgementTop)
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
