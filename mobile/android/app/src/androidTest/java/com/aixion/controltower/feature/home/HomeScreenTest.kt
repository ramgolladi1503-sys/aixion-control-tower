package com.aixion.controltower.feature.home

import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.Density
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class HomeScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun lastSuccessfulSyncRemainsReadableAtLargeFontScale() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                HomeSyncTimestamp(Instant.parse("2026-10-22T10:00:00Z"))
            }
        }

        val timestamp = composeRule.onNodeWithText("Last successful sync", substring = true)
        timestamp.assertExists()
        val timestampBounds = timestamp.fetchSemanticsNode().boundsInRoot
        assertTrue("Timestamp should have visible layout bounds", timestampBounds.width > 0f && timestampBounds.height > 0f)
        assertTrue("Timestamp should scale to at least 40dp high", timestampBounds.height >= 40f)
    }

    @Test
    fun homePreviewShowsThreeRequestsAndOffersSeeAllRoute() {
        val approvals = (1..4).map { index ->
            ApprovalSummary(
                id = "approval-$index",
                projectName = "Project $index",
                title = "Approval $index",
                summary = "Review request $index.",
                agentName = "Antigravity",
                targetBranch = "main",
                status = ApprovalStatus.REQUESTED,
                risk = RiskLevel.MEDIUM,
                files = emptyList(),
                requiredActions = emptyList(),
                testPlan = emptyList(),
                rollbackPlan = ""
            )
        }
        var selectedApprovalId: String? = null
        var openedApprovals = false

        composeRule.setContent {
            HomeApprovalPreview(
                approvals = approvals,
                onApprovalSelected = { selectedApprovalId = it.id },
                onOpenApprovals = { openedApprovals = true }
            )
        }

        composeRule.onNodeWithText("Approval 1").assertExists()
        composeRule.onNodeWithText("Approval 2").assertExists()
        composeRule.onNodeWithText("Approval 3").assertExists()
        composeRule.onAllNodesWithText("Approval 4").assertCountEquals(0)
        composeRule.onNodeWithText("Approval 3").performClick()
        composeRule.runOnIdle { assertEquals("approval-3", selectedApprovalId) }
        composeRule.onNodeWithText("See all approvals").performClick()
        composeRule.runOnIdle { assertEquals(true, openedApprovals) }
    }

    @Test
    fun connectionPanelExplainsLocalRequirementAndOpensPlatformSetup() {
        var opened = false
        composeRule.setContent {
            HomeAgentConnectionPanel(onOpenConnect = { opened = true })
        }

        composeRule.onNodeWithText("Connect an agent").assertExists()
        composeRule.onNodeWithText("Keep that computer online while approvals are pending.", substring = true)
            .assertExists()
        composeRule.onNodeWithText("Connect platform").performClick()
        composeRule.runOnIdle { assertTrue(opened) }
    }
}
