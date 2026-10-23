package com.aixion.controltower.feature.approvals

import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ApprovalRiskConfirmationUiTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun highRiskApprovalRequiresDialogConfirmationBeforeSubmitting() {
        val approval = approval(RiskLevel.HIGH)
        var decisionCount = 0
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval,
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = { decisionCount += 1 },
                onReject = {}
            )
        }

        composeRule.onNodeWithText("Approve request").assertIsEnabled().performClick()
        composeRule.onNodeWithText("Confirm high-risk approval").assertExists()
        composeRule.onNodeWithText("High impact operation", substring = true).assertExists()
        composeRule.onNodeWithText("Target: Production workspace · release", substring = true).assertExists()
        composeRule.onNodeWithText("Risk: High", substring = true).assertExists()
        composeRule.onNodeWithText("Provider: MANUAL", substring = true).assertExists()
        composeRule.onNodeWithText("Connector installation: Not provided", substring = true).assertExists()
        composeRule.runOnIdle { assertEquals("Opening confirmation must not submit a decision", 0, decisionCount) }

        composeRule.onNodeWithText("Approve this request").assertIsEnabled().performClick()
        composeRule.runOnIdle { assertEquals("Only explicit confirmation submits", 1, decisionCount) }
    }

    @Test
    fun externalHighRiskConfirmationRepeatsInvocationIdentity() {
        val approval = approval(RiskLevel.CRITICAL).copy(
            sourceProvider = "ANTIGRAVITY",
            sourceAgentId = "connector-installation-07",
            sourceSessionId = "session-42",
            sourceInvocationId = "antigravity:session-42:tool-991"
        )
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval,
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = {}
            )
        }

        composeRule.onNodeWithText("Approve request").performClick()
        composeRule.onNodeWithText("Confirm high-risk approval").assertExists()
        composeRule.onNodeWithText("Provider: ANTIGRAVITY", substring = true).assertExists()
        composeRule.onNodeWithText("Connector installation: connector-installation-07", substring = true)
            .assertExists()
        composeRule.onNodeWithText("Session: session-42", substring = true).assertExists()
        composeRule.onNodeWithText("Invocation: antigravity:session-42:tool-991", substring = true)
            .assertExists()
    }

    @Test
    fun highRiskConfirmationDisablesIfApprovalIsNoLongerEligible() {
        val approval = mutableStateOf(approval(RiskLevel.CRITICAL))
        var decisionCount = 0
        composeRule.setContent {
            ApprovalDetailContent(
                approval = approval.value,
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = { decisionCount += 1 },
                onReject = {}
            )
        }

        composeRule.onNodeWithText("Approve request").assertIsEnabled().performClick()
        composeRule.onNodeWithText("Confirm high-risk approval").assertExists()
        composeRule.runOnIdle {
            approval.value = approval.value.copy(status = ApprovalStatus.EXPIRED)
        }
        composeRule.waitForIdle()

        composeRule.onNodeWithText("Approve this request").assertIsNotEnabled()
        composeRule.runOnIdle { assertEquals("An expired request must not submit", 0, decisionCount) }
    }
}

private fun approval(risk: RiskLevel) = ApprovalSummary(
    id = "high-risk-approval-test",
    projectName = "Production workspace",
    title = "High impact operation",
    summary = "Replace a production service configuration.",
    agentName = "Antigravity",
    targetBranch = "release",
    status = ApprovalStatus.REQUESTED,
    risk = risk,
    files = listOf(FileChangeSummary("service.yaml", "modify", "update service", risk)),
    requiredActions = emptyList(),
    testPlan = listOf("Run deployment validation"),
    rollbackPlan = "Restore the previous service configuration",
    sourceProvider = "MANUAL",
    expiresAt = Instant.now().plusSeconds(600).toString()
)
