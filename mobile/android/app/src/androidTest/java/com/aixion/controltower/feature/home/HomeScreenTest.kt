package com.aixion.controltower.feature.home

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
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
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class HomeScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun compactHomeHeroGivesLargeTextFullWidthAndKeepsItsAnnouncementSemantics() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    Box(
                        modifier = Modifier
                            .width(320.dp)
                            .height(900.dp)
                            .testTag("home-hero-viewport")
                    ) {
                        HomeContent(
                            state = HomeUiState(
                                loading = false,
                                lastSuccessfulSyncAt = Instant.parse("2026-10-22T10:00:00Z")
                            )
                        )
                    }
                }
            }
        }

        val title = composeRule.onNodeWithTag("home-status-title")
        title.assertIsDisplayed()
        composeRule.onNodeWithTag("home-hero-logo").assertDoesNotExist()
        val viewport = composeRule.onNodeWithTag("home-hero-viewport").fetchSemanticsNode().boundsInRoot
        val titleBounds = title.fetchSemanticsNode().boundsInRoot
        assertTrue("Large Home status text should remain inside compact screen padding", titleBounds.left >= viewport.left + 20f)
        assertTrue("Large Home status text should use the available compact width", titleBounds.right <= viewport.right - 20f)
        assertTrue("Large Home status heading should receive the full hero copy column", titleBounds.width >= 220f)
        assertTrue("The heading should wrap naturally at 200% font scale", titleBounds.height > 72f)
        assertTrue("The status remains a heading", title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading))
        assertEquals(
            "The status remains announced politely when it changes",
            LiveRegionMode.Polite,
            title.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
    }

    @Test
    fun approvalCardSeparatesTrustRiskAndProjectContextAtCompactWidth() {
        val request = ApprovalSummary(
            id = "approval-card-layout",
            projectName = "A long project name that should wrap without crowding the trust badges",
            title = "Allow .env file access",
            summary = "A tool wants to read a local environment file.",
            agentName = "Antigravity",
            targetBranch = "main",
            status = ApprovalStatus.REQUESTED,
            risk = RiskLevel.HIGH,
            files = emptyList(),
            requiredActions = emptyList(),
            testPlan = emptyList(),
            rollbackPlan = "",
            sourceProvider = "ANTIGRAVITY",
            sourceAgentName = "Antigravity Desktop Workspace Connector",
            verifiedSource = true
        )

        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(320.dp)) {
                    ApprovalCard(approval = request)
                }
            }
        }

        val source = composeRule.onNodeWithTag("approval-card-source-badge").assertIsDisplayed()
        val risk = composeRule.onNodeWithTag("approval-card-risk-badge").assertIsDisplayed()
        val project = composeRule.onNodeWithTag("approval-card-project").assertIsDisplayed()
        val title = composeRule.onNodeWithText("Allow .env file access").assertIsDisplayed()
        assertTrue(
            "Each approval card title should be exposed as a heading for direct screen-reader navigation",
            title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        val projectTop = project.fetchSemanticsNode().boundsInRoot.top
        assertTrue("Project context must sit below the trust badge", projectTop >= source.fetchSemanticsNode().boundsInRoot.bottom)
        assertTrue("Project context must sit below the risk badge", projectTop >= risk.fetchSemanticsNode().boundsInRoot.bottom)
        assertTrue("Request title must follow its project context", title.fetchSemanticsNode().boundsInRoot.top >= project.fetchSemanticsNode().boundsInRoot.bottom)
    }

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

        val connectionHeading = composeRule.onNodeWithText("Agent connections").assertExists()
        assertTrue(
            "Agent connection setup should be available through screen-reader heading navigation",
            connectionHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onNodeWithText(
            "Set up Antigravity or Codex on the computer running the project. Keep that computer available so a phone decision can return to the waiting task."
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
