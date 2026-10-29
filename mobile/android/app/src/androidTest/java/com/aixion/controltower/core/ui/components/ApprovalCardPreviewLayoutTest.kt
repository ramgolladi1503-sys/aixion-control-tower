package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalCardPreviewLayoutTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun overviewPreviewUsesTwoLinesForContextAndRemainsBounded() {
        val longSummary = "Review the requested workspace changes, check their purpose, " +
            "confirm the target files, then decide whether the agent may continue."
        val summary = mutableStateOf(longSummary)
        val approval = ApprovalSummary(
            id = "approval_preview_layout",
            projectName = "Project",
            title = "Review workspace request",
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
                    ApprovalCard(approval = approval.copy(summary = summary.value))
                }
            }
        }

        val preview = composeRule.onNodeWithTag("approval-card-preview-content")
        val longHeight = preview.fetchSemanticsNode().boundsInRoot.height
        composeRule.runOnIdle { summary.value = "Review changes." }
        composeRule.waitForIdle()
        val shortHeight = preview.fetchSemanticsNode().boundsInRoot.height
        val maximumTwoLineHeightDelta = with(composeRule.density) { 48.dp.toPx() }

        assertTrue(
            "A useful overview should occupy more than one text line at phone width",
            longHeight > shortHeight + 8f
        )
        assertTrue(
            "The preview should remain capped at two lines instead of growing with arbitrary input",
            longHeight < shortHeight + maximumTwoLineHeightDelta
        )
    }
}
