package com.aixion.controltower.feature.auth

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class AccountInfoPanelTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun accountPanelShowsSessionIdentityAndLogoutWithoutImplementationCopy() {
        var logoutRequested = false
        composeRule.setContent {
            ControlTowerTheme {
                AccountInfoPanel(
                    state = AuthUiState(
                        authenticated = true,
                        userLabel = "Owner · owner@example.com"
                    ),
                    onLogout = { logoutRequested = true }
                )
            }
        }

        composeRule.onNodeWithText("Account information").assertExists()
        composeRule.onNodeWithText("Owner · owner@example.com").assertExists()
        composeRule.onNodeWithText("Logout").assertExists().performClick()
        composeRule.onNodeWithText("Registration, login, and verification controls are hidden because this session is already active.")
            .assertDoesNotExist()
        composeRule.runOnIdle { assertTrue(logoutRequested) }
    }
}
