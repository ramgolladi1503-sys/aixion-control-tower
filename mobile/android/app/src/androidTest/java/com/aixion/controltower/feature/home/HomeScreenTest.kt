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
    fun connectionPanelOffersNeutralManagementNavigationWithoutClaimingStatus() {
        var opened = false
        composeRule.setContent {
            HomeAgentConnectionPanel(onOpenConnect = { opened = true })
        }

        composeRule.onNodeWithText("Agent connections").assertExists()
        composeRule.onNodeWithText("View setup guidance for Antigravity and Codex", substring = true)
            .assertExists()
        composeRule.onAllNodesWithText("Connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Online").assertCountEquals(0)
        composeRule.onNodeWithText("Manage connections").performClick()
        composeRule.runOnIdle { assertTrue(opened) }
    }

    @Test
    fun successfulEmptyHomeShowsOneBackendOnlyQueueState() {
        composeRule.setContent {
            HomeContent(
                state = HomeUiState(
                    loading = false,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T10:00:00Z")
                )
            )
        }

        composeRule.onNodeWithText("Backend synced").assertExists()
        composeRule.onAllNodesWithText("No approvals waiting").assertCountEquals(1)
        composeRule.onAllNodesWithText("All clear").assertCountEquals(0)
        composeRule.onAllNodesWithText("Command state").assertCountEquals(0)
        composeRule.onNodeWithText("Agent connection status is not available here.", substring = true)
            .assertExists()
    }
}
