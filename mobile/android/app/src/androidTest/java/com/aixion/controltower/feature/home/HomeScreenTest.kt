package com.aixion.controltower.feature.home

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
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
                Box(modifier = Modifier.width(320.dp).testTag("timestamp-viewport")) {
                    Box(modifier = Modifier.fillMaxWidth().padding(horizontal = 20.dp)) {
                        HomeSyncTimestamp(Instant.parse("2026-10-22T10:00:00Z"))
                    }
                }
            }
        }

        val timestamp = composeRule.onNodeWithText("Last successful sync", substring = true)
        timestamp.assertExists()
        val viewportBounds = composeRule.onNodeWithTag("timestamp-viewport").fetchSemanticsNode().boundsInRoot
        val timestampBounds = timestamp.fetchSemanticsNode().boundsInRoot
        assertTrue("Timestamp should have visible layout bounds", timestampBounds.width > 0f && timestampBounds.height > 0f)
        assertTrue("Timestamp should wrap and remain within the compact viewport", timestampBounds.left >= viewportBounds.left + 20f && timestampBounds.right <= viewportBounds.right - 20f)
        assertTrue("Large text should occupy a readable multi-line layout", timestampBounds.height > 20f)
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
    fun connectionPanelOffersConnectActionWithoutClaimingConnectionStatus() {
        var opened = false
        composeRule.setContent {
            HomeAgentConnectionPanel(onOpenConnect = { opened = true })
        }

        composeRule.onNodeWithText("Agent connections").assertExists()
        composeRule.onNodeWithText(
            "Set up Antigravity or Codex for the project on the computer where the agent runs. Keep that computer online while approvals are pending."
        ).assertExists()
        composeRule.onAllNodesWithText("manage the computers", substring = true).assertCountEquals(0)
        composeRule.onAllNodesWithText("Connected").assertCountEquals(0)
        composeRule.onAllNodesWithText("Online").assertCountEquals(0)
        composeRule.onNodeWithText("Connect an agent").assertExists().performClick()
        composeRule.onAllNodesWithText("Manage connections").assertCountEquals(0)
        composeRule.onAllNodesWithText("Set up an agent").assertCountEquals(0)
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
        composeRule.onNodeWithText("Synced").assertDoesNotExist()
        composeRule.onAllNodesWithText("No approvals waiting").assertCountEquals(1)
        assertTrue(
            "Home status should be exposed as a screen-reader heading",
            composeRule.onNodeWithText("No approvals waiting")
                .fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onAllNodesWithText("All clear").assertCountEquals(0)
        composeRule.onAllNodesWithText("Command state").assertCountEquals(0)
        composeRule.onNodeWithText("Agent connection status is not available here.", substring = true)
            .assertExists()
    }

    @Test
    fun approvalSummaryAnnouncesRefreshCompletionAndNewRequestCount() {
        val state = mutableStateOf(HomeUiState(loading = true))
        composeRule.setContent {
            HomeContent(state = state.value)
        }

        val summary = composeRule.onNodeWithText("Loading approvals")
        assertEquals(
            androidx.compose.ui.semantics.LiveRegionMode.Polite,
            summary.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )

        composeRule.runOnIdle {
            state.value = HomeUiState(
                loading = false,
                approvals = listOf("first", "second").map { id ->
                    ApprovalSummary(
                        id = "screen-reader-$id",
                        projectName = "Project",
                        title = "Review request",
                        summary = "This request is waiting for a decision.",
                        agentName = "Antigravity",
                        targetBranch = "main",
                        status = ApprovalStatus.REQUESTED,
                        risk = RiskLevel.MEDIUM,
                        files = emptyList(),
                        requiredActions = emptyList(),
                        testPlan = emptyList(),
                        rollbackPlan = ""
                    )
                },
                lastSuccessfulSyncAt = Instant.parse("2026-10-22T10:00:00Z")
            )
        }

        composeRule.onNodeWithText("2 requests require review").assertExists()
        assertEquals(
            androidx.compose.ui.semantics.LiveRegionMode.Polite,
            composeRule.onNodeWithText("2 requests require review")
                .fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
    }

    @Test
    fun backendFailureShowsOneActionableErrorAndHidesDiagnosticsUntilRequested() {
        var retryRequested = false
        composeRule.setContent {
            HomeContent(
                state = HomeUiState(
                    loading = false,
                    errorMessage = "SocketTimeoutException: gateway did not respond"
                ),
                onRetry = { retryRequested = true }
            )
        }

        composeRule.onAllNodesWithText("Approval service unavailable").assertCountEquals(0)
        composeRule.onAllNodesWithText("Service unavailable").assertCountEquals(0)
        composeRule.onAllNodesWithText("We couldn't load your approvals", substring = true)
            .assertCountEquals(0)
        composeRule.onAllNodesWithText("Current approvals unavailable").assertCountEquals(1)
        assertTrue(
            "Home error title should be exposed as a screen-reader heading",
            composeRule.onNodeWithText("Current approvals unavailable")
                .fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onAllNodesWithText(
            "Check your connection, then retry to load the latest requests."
        ).assertCountEquals(1)
        composeRule.onAllNodesWithText("Retry sync").assertCountEquals(1)
        composeRule.onAllNodesWithText("SocketTimeoutException: gateway did not respond")
            .assertCountEquals(0)
        composeRule.onNodeWithText("Technical details").performClick()
        composeRule.onNodeWithText("SocketTimeoutException: gateway did not respond").assertExists()
        composeRule.onNodeWithText("Retry sync").performClick()
        composeRule.runOnIdle { assertTrue(retryRequested) }
    }
}
