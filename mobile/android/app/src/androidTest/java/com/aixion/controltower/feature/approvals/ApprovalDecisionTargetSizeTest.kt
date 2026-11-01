package com.aixion.controltower.feature.approvals

import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ApprovalDecisionTargetSizeTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun approveAndDenyDecisionTargetsAreAtLeast56DpHigh() {
        var density = 1f
        val approval = ApprovalSummary(
            id = "decision-target-size-test",
            projectName = "Test project",
            title = "Review target size",
            summary = "A pending approval used to check the decision controls.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.LOW,
            files = listOf(FileChangeSummary("README.md", "update", "Summary", RiskLevel.LOW)),
            requiredActions = emptyList(),
            testPlan = listOf("Run focused UI tests"),
            rollbackPlan = "Revert this test change",
            sourceProvider = "MANUAL",
            expiresAt = Instant.now().plusSeconds(10 * 60).toString()
        )

        composeRule.setContent {
            density = LocalDensity.current.density
            ApprovalDetailContent(
                approval = approval,
                lastActionMessage = null,
                onOpenDiff = {},
                onApprove = {},
                onReject = {}
            )
        }

        listOf(
            "Deny request ize-test from Manual: Review target size. Risk: low.",
            "Approve request ize-test from Manual: Review target size. Risk: low."
        ).forEach { actionLabel ->
            val heightInDp = composeRule.onNodeWithContentDescription(actionLabel)
                .fetchSemanticsNode()
                .boundsInRoot
                .height / density

            assertTrue("$actionLabel target was ${heightInDp}dp high", heightInDp >= 56f)
        }
    }
}
