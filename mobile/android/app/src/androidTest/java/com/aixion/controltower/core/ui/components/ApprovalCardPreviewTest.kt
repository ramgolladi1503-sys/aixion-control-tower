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
}
