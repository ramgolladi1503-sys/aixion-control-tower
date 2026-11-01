package com.aixion.controltower.feature.approvals

import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ApprovalDecisionActionAccessibilityTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun decisionActionsAnnounceRequestAndRiskWithoutChangingVisibleLabels() {
        val approval = ApprovalSummary(
            id = "approval-accessibility-test",
            projectName = "Payments service",
            title = "Run protected production command",
            summary = "Inspect the exact command before deciding.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.HIGH,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = listOf("Review command output"),
            rollbackPlan = "No change",
            expiresAt = "2099-01-01T00:00:00Z"
        )

        composeRule.setContent {
            Box(modifier = Modifier.width(400.dp).height(1_000.dp)) {
                ApprovalDetailContent(approval, null, {}, {}, {})
            }
        }

        val reject = composeRule.onNodeWithText("Reject")
        val approve = composeRule.onNodeWithText("Approve request")
        reject.assertIsEnabled()
        approve.assertIsEnabled()
        assertEquals(
            listOf("Reject Run protected production command. Risk level: High."),
            reject.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
        )
        assertEquals(
            listOf("Approve Run protected production command. Risk level: High."),
            approve.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
        )
    }
}
