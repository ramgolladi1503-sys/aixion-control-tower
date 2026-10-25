package com.aixion.controltower.feature.auth

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.autofill.ContentType
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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
        composeRule.onNodeWithText("Sign in").assertExists()
        composeRule.onNodeWithText("Register").assertExists()
        assertTrue(
            "Sign-in title should be exposed as a screen-reader heading",
            composeRule.onNodeWithText("Welcome back")
                .fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onNodeWithText("Name").assertDoesNotExist()
        composeRule.onNodeWithText("Invite code (optional)").assertDoesNotExist()

        composeRule.onNodeWithText("Register").performScrollTo().performClick()

        composeRule.onNodeWithText("Name").assertDoesNotExist()
        composeRule.onNodeWithText("Invite code (optional)").assertDoesNotExist()
        composeRule.onNodeWithText("Email address").assertExists()
        composeRule.onNodeWithText("Password").assertExists()
        composeRule.onNodeWithText("Back to sign in").assertExists()
        composeRule.onNodeWithContentDescription("Show password").assertExists()
    }

    @Test
    fun emailAndPasswordFieldsExposeSignInAndRegistrationAutofillTypes() {
        composeRule.setContent {
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

        composeRule.onNodeWithText("Email address").assert(
            SemanticsMatcher.expectValue(SemanticsProperties.ContentType, ContentType.EmailAddress)
        )
        composeRule.onNodeWithText("Password").assert(
            SemanticsMatcher.expectValue(SemanticsProperties.ContentType, ContentType.Password)
        )

        composeRule.onNodeWithText("Create account").performClick()
        composeRule.onNodeWithText("Email address").assert(
            SemanticsMatcher.expectValue(SemanticsProperties.ContentType, ContentType.EmailAddress)
        )
        composeRule.onNodeWithText("Password").assert(
            SemanticsMatcher.expectValue(SemanticsProperties.ContentType, ContentType.NewPassword)
        )
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

        composeRule.onNodeWithContentDescription("Show password")
            .assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Password is hidden"))
            .performClick()
        composeRule.onNodeWithContentDescription("Show password").assertDoesNotExist()
        composeRule.onNodeWithContentDescription("Hide password")
            .assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Password is visible"))
            .performClick()
        composeRule.onNodeWithContentDescription("Show password")
            .assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Password is hidden"))

        composeRule.onNodeWithText("Create account").performClick()
        composeRule.onNodeWithContentDescription("Show password").assertExists().performClick()
        composeRule.onNodeWithContentDescription("Hide password").assertExists()

        composeRule.runOnIdle {
            assertEquals("Visibility toggles preserve the field value", "entered-secret", state.value.password)
            assertFalse("Visibility toggles do not emit password edits", passwordChanged)
        }
    }

    @Test
    fun loginFailureIsAnnouncedAsErrorInsteadOfNeutralStatus() {
        val legacySessionMessage = "Login failed: Session expired or invalid. Open Acct and log in again."
        val message = "Your session expired. Log in again to continue."
        composeRule.setContent {
            AuthFormContent(
                state = AuthUiState(
                    message = legacySessionMessage,
                    messageSeverity = AuthMessageSeverity.ERROR
                ),
                onEmailChange = {},
                onPasswordChange = {},
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.onNodeWithText(message).assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Error"))
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.LiveRegion, LiveRegionMode.Polite))
    }

    @Test
    fun longAuthFailureCopyWrapsInsideAvailableWidth() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                AuthFeedbackMessage(
                    message = "Your session expired. Log in again to continue.",
                    severity = AuthMessageSeverity.ERROR
                )
            }
        }

        val feedback = composeRule.onNodeWithText("Your session expired. Log in again to continue.")
        feedback.assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Error"))
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.LiveRegion, LiveRegionMode.Polite))
        val bounds = feedback.fetchSemanticsNode().boundsInRoot
        assertTrue("Feedback should stay within the narrow screen width", bounds.width <= 240f)
        assertTrue("Feedback should wrap into a readable multi-line block", bounds.height >= 36f)
    }

    @Test
    fun signInProgressIsAnnouncedToTalkBackWhileTheRequestIsInFlight() {
        composeRule.setContent {
            AuthFormContent(
                state = AuthUiState(loading = true),
                onEmailChange = {},
                onPasswordChange = {},
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.onNodeWithText("Signing in…")
            .assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Information"))
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.LiveRegion, LiveRegionMode.Polite))
        composeRule.onNodeWithText("Email address").assertIsNotEnabled()
        composeRule.onNodeWithText("Password").assertIsNotEnabled()
        composeRule.onNodeWithText("Sign in").assertIsNotEnabled()
        composeRule.onNodeWithText("Register").assertIsNotEnabled()
    }

    @Test
    fun verificationInputsStayLockedAndProgressRemainsExplicitWhileSubmitting() {
        composeRule.setContent {
            AuthFormContent(
                state = AuthUiState(
                    email = "owner@example.com",
                    verificationRequired = true,
                    verificationCode = "123456",
                    loading = true
                ),
                onEmailChange = {},
                onPasswordChange = {},
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.onNodeWithText("Email address").assertIsNotEnabled()
        composeRule.onNodeWithText("Verification code").assertIsNotEnabled()
        composeRule.onNodeWithText("Verify email").assertIsNotEnabled()
        composeRule.onNodeWithText("Resend code").assertIsNotEnabled()
        composeRule.onNodeWithText("Sign in").assertDoesNotExist()
        composeRule.onNodeWithText("Register").assertDoesNotExist()
        composeRule.onNodeWithText("Working…")
            .assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.LiveRegion, LiveRegionMode.Polite))
    }

    @Test
    fun credentialFieldsAndPrimaryActionAreAvailableWhenNotSubmitting() {
        composeRule.setContent {
            AuthFormContent(
                state = AuthUiState(email = "owner@example.com", password = "StrongPass123!"),
                onEmailChange = {},
                onPasswordChange = {},
                onVerificationCodeChange = {},
                onLogin = {},
                onRegister = {},
                onVerify = {},
                onResendVerification = {}
            )
        }

        composeRule.onNodeWithText("Email address").assertIsEnabled()
        composeRule.onNodeWithText("Password").assertIsEnabled()
        composeRule.onNodeWithText("Sign in").assertIsEnabled()
        composeRule.onNodeWithText("Register").assertIsEnabled()
    }

    @Test
    fun loginAndRegistrationActionsRemainScrollableAtTwoHundredPercentText() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp)) {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
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
            }
        }

        val signInHeading = composeRule.onNodeWithText("Welcome back")
        signInHeading.assertExists()
        composeRule.runOnIdle {
            val bounds = signInHeading.fetchSemanticsNode().boundsInRoot
            assertTrue("Sign-in heading must stay within narrow viewport", bounds.right <= 240f)
            assertTrue("Sign-in heading must wrap at 200% text", bounds.height > 48f)
        }
        composeRule.onNodeWithText("Sign in").performScrollTo().assertExists()
        composeRule.onNodeWithText("Register").performScrollTo().performClick()
        composeRule.onNodeWithText("Create your account").assertExists()
        composeRule.onNodeWithText("Create account").performScrollTo().assertExists()
        composeRule.onNodeWithText("Back to sign in").performScrollTo().assertExists()
    }
}
