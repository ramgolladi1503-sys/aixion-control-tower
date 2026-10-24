package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.dp
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
        val path = composeRule.onNodeWithText("codex://tool/shell")
        val exactPayload = composeRule.onNodeWithText(payload)
        val acknowledgement = composeRule.onNodeWithTag("approval-exact-tool-review-acknowledgement")

        context.assertExists()
        path.assertExists()
        exactPayload.assertExists()
        acknowledgement.assertExists()
        context.assertTextContains("Provider: Codex · Project: Mobile app workspace")

        val contextTop = context.fetchSemanticsNode().boundsInRoot.top
        val payloadTop = exactPayload.fetchSemanticsNode().boundsInRoot.top
        val acknowledgementTop = acknowledgement.fetchSemanticsNode().boundsInRoot.top
        assertTrue("Source context must precede the payload", contextTop < payloadTop)
        assertTrue("The review acknowledgement must follow the payload", payloadTop < acknowledgementTop)
    }
}
