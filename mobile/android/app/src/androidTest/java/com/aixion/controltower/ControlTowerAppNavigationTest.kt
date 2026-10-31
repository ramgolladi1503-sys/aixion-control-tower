package com.aixion.controltower

import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.dp
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.feature.approvals.ApprovalsUiState
import com.aixion.controltower.navigation.Route
import org.junit.Rule
import org.junit.Test

/** Exercises the production bottom navigation and tab-switch policy with a real Compose NavHost. */
class ControlTowerAppNavigationTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun selectingHomeFromConnectAndAccountReturnsToHomeAndOtherTabsRemainReachable() {
        composeRule.setContent {
            ControlTowerTheme {
                PrimaryNavigationTestHost()
            }
        }

        assertCurrentRoute("Home")

        composeRule.onNodeWithText(Route.Connectors.label).performClick()
        assertCurrentRoute("Connect")

        composeRule.onNodeWithText(Route.Home.label).performClick()
        assertCurrentRoute("Home")

        composeRule.onNodeWithText(Route.Account.label).performClick()
        assertCurrentRoute("Account")

        composeRule.onNodeWithText(Route.Home.label).performClick()
        assertCurrentRoute("Home")

        composeRule.onNodeWithText(Route.Connectors.label).performClick()
        assertCurrentRoute("Connect")
        composeRule.onNodeWithTag("primary-navigation-bar").assertExists()
    }

    private fun assertCurrentRoute(expected: String) {
        composeRule.waitForIdle()
        composeRule.onNodeWithTag("current-primary-route").assertTextEquals("Current: $expected")
    }
}

@Composable
private fun PrimaryNavigationTestHost() {
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route ?: Route.Home.value

    Scaffold(
        bottomBar = {
            AdaptivePrimaryNavigation(
                expanded = false,
                currentRoute = currentRoute,
                approvalsState = ApprovalsUiState(loading = false),
                onNavigate = { target ->
                    navigatePrimaryDestination(navController, currentRoute, target)
                }
            )
        }
    ) { innerPadding ->
        BoxWithConstraints(
            modifier = Modifier
                .padding(innerPadding)
                .fillMaxSize()
        ) {
            NavHost(
                navController = navController,
                startDestination = Route.Home.value,
                modifier = Modifier.fillMaxSize()
            ) {
                listOf(Route.Home, Route.Inbox, Route.Connectors, Route.Account).forEach { route ->
                    composable(route.value) {
                        Text(
                            text = "Current: ${route.label}",
                            modifier = Modifier.testTag("current-primary-route")
                        )
                    }
                }
            }
        }
    }
}
