package com.aixion.controltower

import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onAllNodesWithContentDescription
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.TowerLayout
import com.aixion.controltower.navigation.Route
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.feature.approvals.ApprovalsUiState
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ControlTowerAdaptiveNavigationTest {
    @get:Rule
    val composeRule = createComposeRule()

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
            listOf(390, 840).forEach { width ->
                renderNavigation(width = width, approvalsState = state)
                composeRule.waitForIdle()
                composeRule.onAllNodesWithTag("approval-navigation-badge").assertCountEquals(0)
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

    private fun renderNavigation(
        width: Int,
        approvalsState: ApprovalsUiState = ApprovalsUiState(loading = false)
    ) {
        selectedRoute = null
        composeRule.setContent {
            ControlTowerTheme {
                BoxWithConstraints(
                    modifier = Modifier.width(width.dp).height(600.dp).testTag("navigation-viewport")
                ) {
                    AdaptivePrimaryNavigation(
                        expanded = maxWidth >= TowerLayout.expandedBreakpoint,
                        currentRoute = Route.Home.value,
                        approvalsState = approvalsState,
                        onNavigate = { selectedRoute = it }
                    )
                }
            }
        }
    }

    private fun assertBadgeForBothLayouts(
        state: ApprovalsUiState,
        expectedText: String,
        expectedDescription: String
    ) {
        listOf(390, 840).forEach { width ->
            renderNavigation(width = width, approvalsState = state)
            composeRule.waitForIdle()
            composeRule.onNodeWithTag("approval-navigation-badge").assertIsDisplayed()
            composeRule.onNodeWithText(expectedText, useUnmergedTree = true).assertIsDisplayed()
            composeRule.onNodeWithContentDescription(expectedDescription).assertIsDisplayed()
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
