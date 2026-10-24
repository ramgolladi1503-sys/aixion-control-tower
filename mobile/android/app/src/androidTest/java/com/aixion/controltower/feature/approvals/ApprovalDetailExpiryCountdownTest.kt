package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Rule
import org.junit.Test

class ApprovalDetailExpiryCountdownTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun decisionPanelKeepsExpiryCountdownBesideDecisionControls() {
        val now = Instant.now()
        val request = ApprovalSummary(
            id = "expiry-countdown-test",
            projectName = "Workspace",
            title = "Run focused check",
            summary = "Review this approval before deciding.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = listOf(FileChangeSummary("check.txt", "update", "+ok", RiskLevel.LOW)),
            requiredActions = emptyList(),
            testPlan = listOf("Run focused check"),
            rollbackPlan = "Revert the check.",
            sourceProvider = "ANTIGRAVITY",
            sourceAgentId = "agent-expiry-test",
            sourceSessionId = "session-expiry-test",
            sourceInvocationId = "invocation-expiry-test",
            expiresAt = now.plusSeconds(6 * 60 * 60).toString(),
            verifiedSource = true
        )

        composeRule.setContent {
            ApprovalDetailContent(request, null, {}, {}, {})
        }

        composeRule.onNodeWithTag("approval-decision-expiry-countdown")
            .assertExists()
            .assertTextContains("Expires in about 6 hours")
    }
}
