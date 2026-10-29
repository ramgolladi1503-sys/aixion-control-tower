package com.aixion.controltower

import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.TowerLayout
import com.aixion.controltower.navigation.Route
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.feature.approvals.ApprovalsUiState
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ControlTowerAdaptiveNavigationTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun compactBottomNavigationKeepsFourLabelsWithinBarAtLargeFontScale() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    BoxWithConstraints(
                        modifier = Modifier.width(390.dp).height(600.dp).testTag("navigation-viewport")
                    ) {
                        AdaptivePrimaryNavigation(
                            expanded = false,
                            currentRoute = Route.Home.value,
                            approvalsState = ApprovalsUiState(loading = false),
                            onNavigate = {}
                        )
                    }
                }
            }
        }

        val barBounds = composeRule.onNodeWithTag("primary-navigation-bar")
            .fetchSemanticsNode().boundsInRoot
        listOf(Route.Home, Route.Inbox, Route.Connectors, Route.Account).forEach { route ->
            val label = composeRule.onNodeWithText(route.label)
            label.assertIsDisplayed()
            val labelBounds = label.fetchSemanticsNode().boundsInRoot
            assertTrue("${route.label} label should remain inside the navigation bar", labelBounds.left >= barBounds.left && labelBounds.right <= barBounds.right)
            assertTrue("${route.label} label should not be clipped vertically", labelBounds.top >= barBounds.top && labelBounds.bottom <= barBounds.bottom)
        }
    }

    @Test
    fun compactWidthUsesBottomNavigationWithAllPrimaryDestinations() {
        renderNavigation(width = 390)

        composeRule.onNodeWithTag("primary-navigation-bar").assertIsDisplayed()
        composeRule.onAllNodesWithTag("primary-navigation-rail").assertCountEquals(0)
        assertPrimaryDestinationsDisplayed()
    }

    @Test
    fun expandedWidthUsesRailWithAllPrimaryDestinations() {
        renderNavigation(width = 840)

        composeRule.onNodeWithTag("primary-navigation-rail").assertIsDisplayed()
        composeRule.onAllNodesWithTag("primary-navigation-bar").assertCountEquals(0)
        assertPrimaryDestinationsDisplayed()
    }

    @Test
    fun mediumWidthKeepsBottomNavigationAndSelectingDestinationCallsNavigationHandler() {
        renderNavigation(width = 839)

        composeRule.onNodeWithTag("primary-navigation-bar").assertIsDisplayed()
        composeRule.onNodeWithText(Route.Inbox.label).performClick()
        composeRule.runOnIdle { assertEquals(Route.Inbox, selectedRoute) }
    }

    @Test
    fun compactBarShowsOnlyFreshActionableSupportedApprovalCount() {
        assertBadgeForBothLayouts(
            ApprovalsUiState(loading = false, approvals = listOf(approval("a1"), approval("a2"))),
            expectedText = "2",
            expectedDescription = "Approvals, 2 pending"
        )
    }

    @Test
    fun navigationHidesBadgeForNoActionableRequestsLoadingRefreshAndError() {
        val unsupportedOnly = ApprovalsUiState(
            loading = false,
            approvals = listOf(
                approval("done", status = ApprovalStatus.APPROVED),
                approval("blocked", status = ApprovalStatus.BLOCKED),
                approval("risk-blocked", risk = RiskLevel.BLOCKED),
                approval("unknown", risk = RiskLevel.UNKNOWN)
            )
        )
        val loading = ApprovalsUiState(loading = true, approvals = listOf(approval("stale")))
        val refreshing = ApprovalsUiState(loading = false, refreshing = true, approvals = listOf(approval("stale")))
        val error = ApprovalsUiState(
            loading = false,
            approvals = listOf(approval("cached")),
            errorMessage = "Approval sync unavailable"
        )

        listOf(unsupportedOnly, loading, refreshing, error).forEach { state ->
            listOf(false, true).forEach { expanded ->
                renderNavigation(width = 390, expanded = expanded, approvalsState = state)
                composeRule.waitForIdle()
                composeRule.onAllNodesWithTag("approval-navigation-badge", useUnmergedTree = true)
                    .assertCountEquals(0)
                composeRule.onAllNodesWithContentDescription("Approvals, 1 pending").assertCountEquals(0)
            }
        }
    }

    @Test
    fun navigationCapsVisibleBadgeAndKeepsFullAccessibleCountForBarAndRail() {
        assertBadgeForBothLayouts(
            ApprovalsUiState(loading = false, approvals = (1..100).map { approval("a$it") }),
            expectedText = "99+",
            expectedDescription = "Approvals, 100 pending"
        )
    }

    private var selectedRoute: Route? = null
    private val navigationWidth = mutableIntStateOf(390)
    private val navigationExpanded = mutableStateOf(false)
    private val navigationApprovalsState = mutableStateOf(ApprovalsUiState(loading = false))
    private var navigationContentStarted = false

    private fun renderNavigation(
        width: Int,
        expanded: Boolean? = null,
        approvalsState: ApprovalsUiState = ApprovalsUiState(loading = false)
    ) {
        selectedRoute = null
        if (navigationContentStarted) {
            composeRule.runOnIdle {
                navigationWidth.intValue = width
                navigationExpanded.value = expanded ?: (width.dp >= TowerLayout.expandedBreakpoint)
                navigationApprovalsState.value = approvalsState
            }
        } else {
            navigationWidth.intValue = width
            navigationExpanded.value = expanded ?: (width.dp >= TowerLayout.expandedBreakpoint)
            navigationApprovalsState.value = approvalsState
            navigationContentStarted = true
            composeRule.setContent {
                ControlTowerTheme {
                    BoxWithConstraints(
                        modifier = Modifier.width(navigationWidth.intValue.dp)
                            .height(600.dp)
                            .testTag("navigation-viewport")
                    ) {
                        AdaptivePrimaryNavigation(
                            expanded = navigationExpanded.value,
                            currentRoute = Route.Home.value,
                            approvalsState = navigationApprovalsState.value,
                            onNavigate = { selectedRoute = it }
                        )
                    }
                }
            }
        }
    }

    private fun assertBadgeForBothLayouts(
        state: ApprovalsUiState,
        expectedText: String,
        expectedDescription: String
    ) {
        listOf(false, true).forEach { expanded ->
            renderNavigation(width = 390, expanded = expanded, approvalsState = state)
            composeRule.waitForIdle()
            // Material3 places the badge partly outside the icon's layout bounds.
            // The badge node itself can therefore fail Compose's full-bounds
            // displayed check even though its count text is visible. Assert that
            // the badge is composed, then require the actual count to be displayed
            // and announced below.
            composeRule.onNodeWithTag("approval-navigation-badge", useUnmergedTree = true).assertExists()
            val badgeNode = composeRule.onNodeWithText(expectedText, useUnmergedTree = true)
            badgeNode.assertIsDisplayed()
            val badgeAnnouncement = expectedDescription.substringAfter(", ")
                .replace(" pending", " pending approvals")
            assertEquals(
                listOf(badgeAnnouncement),
                badgeNode.fetchSemanticsNode().config[SemanticsProperties.ContentDescription]
            )
            composeRule.onNodeWithText(Route.Inbox.label)
                .assertIsDisplayed()
                .assert(SemanticsMatcher.expectValue(SemanticsProperties.Role, Role.Tab))
        }
    }

    private fun approval(
        id: String,
        status: ApprovalStatus = ApprovalStatus.REQUESTED,
        risk: RiskLevel = RiskLevel.LOW
    ) = ApprovalSummary(
        id = id,
        projectName = "Project",
        title = "Approval $id",
        summary = "Review this action",
        agentName = "Antigravity",
        targetBranch = "main",
        status = status,
        risk = risk,
        files = emptyList(),
        requiredActions = emptyList(),
        testPlan = emptyList(),
        rollbackPlan = ""
    )

    private fun assertPrimaryDestinationsDisplayed() {
        composeRule.onNodeWithText(Route.Home.label).assertIsDisplayed()
        composeRule.onNodeWithText(Route.Inbox.label).assertIsDisplayed()
        composeRule.onNodeWithText(Route.Connectors.label).assertIsDisplayed()
        composeRule.onNodeWithText(Route.Account.label).assertIsDisplayed()
    }
}
