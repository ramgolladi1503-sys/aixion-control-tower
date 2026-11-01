package com.aixion.controltower.feature.approvals

import androidx.compose.ui.test.assertContentDescriptionEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.LiveRegionMode
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Assert.assertEquals
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

        composeRule.onNodeWithContentDescription("Approval expires in about 6 hours.")
            .assertExists()
            .assertContentDescriptionEquals("Approval expires in about 6 hours.")
    }

    @Test
    fun expiryCountdownAnnouncesUrgencyWithoutExposingTickingCopy() {
        val request = ApprovalSummary(
            id = "expiry-urgent-test",
            projectName = "Workspace",
            title = "Review command",
            summary = "Review this approval before deciding.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "No change is applied until approved.",
            sourceProvider = "ANTIGRAVITY",
            sourceAgentId = "agent-expiry-test",
            sourceSessionId = "session-expiry-test",
            sourceInvocationId = "invocation-expiry-test",
            expiresAt = Instant.now().plusSeconds(4 * 60).toString(),
            verifiedSource = true
        )
        composeRule.setContent { ApprovalDetailContent(request, null, {}, {}, {}) }

        val countdown = composeRule.onNodeWithContentDescription("Approval expires in less than five minutes.")
        countdown.assertExists()
        val semantics = countdown.fetchSemanticsNode().config
        assertEquals(LiveRegionMode.Polite, semantics[SemanticsProperties.LiveRegion])
        assertEquals(
            "Approval expires in less than five minutes.",
            semantics[SemanticsProperties.ContentDescription].single()
        )
    }
}
