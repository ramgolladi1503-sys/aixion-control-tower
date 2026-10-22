package com.aixion.controltower.feature.auth

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.ui.Modifier
import androidx.compose.runtime.mutableStateOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Rule
import org.junit.Test

class AuthFormContentTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun signInStartsWithOnlyEmailPasswordAndLoginAndRevealsRegistrationOnRequest() {
        composeRule.setContent {
            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                AuthFormContent(
                    state = AuthUiState(),
                    onEmailChange = {},
                    onPasswordChange = {},
                    onVerificationCodeChange = {},
                    onLogin = {},
                    onRegister = {},
                    onVerify = {},
                    onResendVerification = {}
                )
            }
        }

        composeRule.onNodeWithText("Email address").assertExists()
        composeRule.onNodeWithText("Password").assertExists()
        composeRule.onNodeWithText("Log in").assertExists()
        composeRule.onNodeWithText("Create account").assertExists()
        composeRule.onNodeWithText("Name").assertDoesNotExist()
        composeRule.onNodeWithText("Invite code (optional)").assertDoesNotExist()

        composeRule.onNodeWithText("Create account").performScrollTo().performClick()

        composeRule.onNodeWithText("Name").assertDoesNotExist()
        composeRule.onNodeWithText("Invite code (optional)").assertDoesNotExist()
        composeRule.onNodeWithText("Email address").assertExists()
        composeRule.onNodeWithText("Password").assertExists()
        composeRule.onNodeWithText("Back to sign in").assertExists()
        composeRule.onNodeWithContentDescription("Show password").assertExists()
    }

    @Test
    fun passwordVisibilityToggleIsAccessibleAndPreservesPasswordInLoginAndRegistration() {
        val state = mutableStateOf(AuthUiState(password = "entered-secret"))
        var passwordChanged = false
        composeRule.setContent {
            AuthFormContent(
                state = state.value,
                onEmailChange = {},
                onPasswordChange = {
                    passwordChanged = true
                    state.value = state.value.copy(password = it)
                },
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.onNodeWithContentDescription("Show password").assertExists().performClick()
        composeRule.onNodeWithContentDescription("Show password").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Hide password").assertExists().performClick()
        composeRule.onNodeWithContentDescription("Show password").assertExists()

        composeRule.onNodeWithText("Create account").performClick()
        composeRule.onNodeWithContentDescription("Show password").assertExists().performClick()
        composeRule.onNodeWithContentDescription("Hide password").assertExists()

        composeRule.runOnIdle {
            assertEquals("Visibility toggles preserve the field value", "entered-secret", state.value.password)
            assertFalse("Visibility toggles do not emit password edits", passwordChanged)
        }
    }
}
