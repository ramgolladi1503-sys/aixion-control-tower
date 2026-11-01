package com.aixion.controltower.feature.auth

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.autofill.AutofillType
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalAutofillTree
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.autofill.AutofillTree
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class AuthFormContentTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun signInFormIsCenteredInTheAvailableViewport() {
        composeRule.setContent {
            ControlTowerTheme {
                Box(modifier = Modifier.width(360.dp).height(800.dp).testTag("auth-screen-viewport")) {
                    AuthFormViewport {
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
            }
        }

        val viewport = composeRule.onNodeWithTag("auth-screen-viewport").fetchSemanticsNode().boundsInRoot
        val form = composeRule.onNodeWithTag("auth-form-content").fetchSemanticsNode().boundsInRoot
        val viewportCenterY = (viewport.top + viewport.bottom) / 2f
        val formCenterY = (form.top + form.bottom) / 2f
        assertTrue("Sign-in form should be vertically centered when it fits", kotlin.math.abs(formCenterY - viewportCenterY) < 48f)
    }

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

    @OptIn(ExperimentalComposeUiApi::class)
    @Suppress("DEPRECATION")
    @Test
    fun emailAndPasswordFieldsExposeCredentialAutofillTargets() {
        val state = mutableStateOf(AuthUiState())
        lateinit var autofillTree: AutofillTree
        composeRule.setContent {
            autofillTree = LocalAutofillTree.current
            AuthFormContent(
                state = state.value,
                onEmailChange = { state.value = state.value.copy(email = it) },
                onPasswordChange = { state.value = state.value.copy(password = it) },
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.runOnIdle {
            val emailNode = autofillTree.children.values.single { AutofillType.EmailAddress in it.autofillTypes }
            val passwordNode = autofillTree.children.values.single { AutofillType.Password in it.autofillTypes }
            emailNode.onFill?.invoke("owner@example.com")
            passwordNode.onFill?.invoke("example-secret")

            assertEquals("owner@example.com", state.value.email)
            assertEquals("example-secret", state.value.password)
        }
    }

    @Test
    fun emailNextMovesToPasswordAndPasswordDoneSubmitsLogin() {
        var loginCount = 0
        composeRule.setContent {
            AuthFormContent(
                state = AuthUiState(email = "owner@example.com", password = "correct-horse-battery"),
                onEmailChange = {},
                onPasswordChange = {},
                onVerificationCodeChange = {},
                onLogin = { loginCount += 1 },
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.onNodeWithText("Email address").performImeAction()
        composeRule.onNodeWithText("Password").assertIsFocused().performImeAction()
        composeRule.runOnIdle { assertEquals(1, loginCount) }
    }

    @Test
    fun authenticationFailureIsShownAsAnAssertiveInlineAnnouncement() {
        composeRule.setContent {
            AuthFormContent(
                state = AuthUiState(message = "Login failed: invalid credentials"),
                onEmailChange = {},
                onPasswordChange = {},
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        val message = composeRule.onNodeWithText("Login failed: invalid credentials")
        message.assertExists()
        assertEquals(
            LiveRegionMode.Assertive,
            message.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
    }
}
