package com.aixion.controltower.feature.auth

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class AuthenticatedAccountContentTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun cancellingAccountRemovalConfirmationDoesNotSubmitRequest() {
        var requestCount = 0
        composeRule.setContent {
            ControlTowerTheme {
                AuthenticatedAccountContent(
                    state = AuthUiState(authenticated = true, userLabel = "Owner · owner@example.com"),
                    onLogout = {},
                    onRemovalReasonChange = {},
                    onRemovalRequest = { requestCount += 1 },
                    showNotificationPermission = false
                )
            }
        }

        composeRule.onNodeWithText("Privacy and data controls").assertExists()
        composeRule.onNodeWithText("Request account removal").performClick()
        composeRule.onNodeWithText("Confirm account removal request?").assertExists()
        composeRule.onNodeWithText("Cancel").performClick()

        composeRule.runOnIdle { assertEquals(0, requestCount) }
        composeRule.onNodeWithText("Confirm account removal request?").assertDoesNotExist()
    }

    @Test
    fun confirmingAccountRemovalSubmitsExactlyOneRequest() {
        var requestCount = 0
        composeRule.setContent {
            ControlTowerTheme {
                AuthenticatedAccountContent(
                    state = AuthUiState(authenticated = true, userLabel = "Owner · owner@example.com"),
                    onLogout = {},
                    onRemovalReasonChange = {},
                    onRemovalRequest = { requestCount += 1 },
                    showNotificationPermission = false
                )
            }
        }

        composeRule.onNodeWithText("Request account removal").assertExists().performClick()
        composeRule.onNodeWithText("Submit request").assertExists().performClick()

        composeRule.runOnIdle { assertEquals(1, requestCount) }
        composeRule.onNodeWithText("Confirm account removal request?").assertDoesNotExist()
    }
}
