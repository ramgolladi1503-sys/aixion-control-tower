package com.aixion.controltower.feature.home

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
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
        val fontScale = mutableStateOf(1f)
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = fontScale.value)) {
                HomeSyncTimestamp(Instant.parse("2026-10-22T10:00:00Z"))
            }
        }
        val baseline = composeRule.onNodeWithText("Last successful sync", substring = true)
            .fetchSemanticsNode().boundsInRoot

        composeRule.runOnIdle { fontScale.value = 2f }
        composeRule.waitForIdle()

        val timestamp = composeRule.onNodeWithText("Last successful sync", substring = true)
        timestamp.assertExists()
        val timestampBounds = timestamp.fetchSemanticsNode().boundsInRoot
        assertTrue("Timestamp should have visible layout bounds", timestampBounds.width > 0f && timestampBounds.height > 0f)
        assertTrue("Timestamp should grow with the user's large-text setting", timestampBounds.height > baseline.height)
    }

    @Test
    fun homeHeroUsesFullCompactWidthAndHidesBrandMarkForLargeText() {
        val fontScale = mutableStateOf(1f)
        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(320.dp).height(520.dp).testTag("home-hero-viewport")) {
                    CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = fontScale.value)) {
                        TowerHeroPanel {
                            HomeStatusHero(
                                HomeUiState(
                                    loading = false,
                                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T10:00:00Z")
                                )
                            )
                        }
                    }
                }
            }
        }
        val baselineHeadingHeight = composeRule.onNodeWithTag("home-status-title")
            .fetchSemanticsNode().boundsInRoot.height

        composeRule.runOnIdle { fontScale.value = 2f }
        composeRule.waitForIdle()

        val viewport = composeRule.onNodeWithTag("home-hero-viewport").fetchSemanticsNode().boundsInRoot
        val title = composeRule.onNodeWithTag("home-status-title").assertIsDisplayed()
        val titleBounds = title.fetchSemanticsNode().boundsInRoot
        assertTrue("Large-text hero heading should remain inside the compact viewport", titleBounds.left >= viewport.left && titleBounds.right <= viewport.right)
        assertTrue("Large system text should scale the hero heading", titleBounds.height > baselineHeadingHeight)
        assertTrue("Hero heading should remain available as a heading", title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading))
        composeRule.onNodeWithTag("home-hero-logo").assertDoesNotExist()
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

        val connectHeading = composeRule.onNodeWithText("Connect an agent")
        connectHeading.assertExists()
        assertTrue(
            "The connection panel title should be exposed as a screen-reader heading",
            connectHeading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onNodeWithText("Keep that computer online while approvals are pending.", substring = true)
            .assertExists()
        composeRule.onNodeWithText("Connect platform").performClick()
        composeRule.runOnIdle { assertTrue(opened) }
    }
}
