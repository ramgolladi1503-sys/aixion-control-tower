package com.aixion.controltower.feature.auth

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithTag
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
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
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
    fun loginStartsWithEmailPasswordAndRegisterAndRevealsRegistrationOnRequest() {
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

        composeRule.onNodeWithText("Logging in…")
            .assertExists()
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.StateDescription, "Information"))
            .assert(SemanticsMatcher.expectValue(SemanticsProperties.LiveRegion, LiveRegionMode.Polite))
        composeRule.onNodeWithText("Email address").assertIsNotEnabled()
        composeRule.onNodeWithText("Password").assertIsNotEnabled()
        composeRule.onNodeWithText("Log in").assertIsNotEnabled()
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
        composeRule.onNodeWithText("Log in").assertDoesNotExist()
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
        composeRule.onNodeWithText("Log in").assertIsEnabled()
        composeRule.onNodeWithText("Register").assertIsEnabled()
    }

    @Test
    fun loginAndRegistrationActionsRemainScrollableAtTwoHundredPercentText() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    Box(modifier = Modifier.width(240.dp).height(420.dp)) {
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
        }

        val signInHeading = composeRule.onNodeWithText("Welcome back")
        signInHeading.assertExists()
        val signInSubtitle = composeRule.onNodeWithText("Sign in to review and decide on pending approvals.")
        val formPanel = composeRule.onNodeWithTag("auth-form-panel")
        signInSubtitle.assertExists()
        formPanel.assertExists()
        val bounds = signInHeading.fetchSemanticsNode().boundsInRoot
        assertTrue("Sign-in heading must stay within narrow viewport", bounds.right <= 240f)
        assertTrue("Sign-in heading must wrap at 200% text", bounds.height > 48f)

        val subtitleBounds = signInSubtitle.fetchSemanticsNode().boundsInRoot
        val formBounds = formPanel.fetchSemanticsNode().boundsInRoot
        assertTrue("The sign-in card must stay within the narrow viewport", formBounds.right <= 240f)
        assertTrue("The form should follow the subtitle with a compact, positive gap", formBounds.top - subtitleBounds.bottom in 0f..10f)
        composeRule.onNodeWithText("Log in").performScrollTo().assertExists()
        composeRule.onNodeWithText("Register").performScrollTo().performClick()
        composeRule.onNodeWithText("Create your account").assertExists()
        composeRule.onNodeWithText("Create account").performScrollTo().assertExists()
        composeRule.onNodeWithText("Back to sign in").performScrollTo().assertExists()
    }

    @Test
    fun signInClusterCentersInAvailableViewportAtDefaultTextScale() {
        composeRule.setContent {
            ControlTowerTheme {
                Box(
                    modifier = Modifier
                        .width(360.dp)
                        .height(800.dp)
                        .testTag("auth-form-viewport")
                ) {
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

        val heading = composeRule.onNodeWithText("Welcome back")
        val register = composeRule.onNodeWithText("Register")
        heading.assertExists()
        register.assertExists()
        val viewportBounds = composeRule.onNodeWithTag("auth-form-viewport")
            .fetchSemanticsNode().boundsInRoot
        val headingBounds = heading.fetchSemanticsNode().boundsInRoot
        val registerBounds = register.fetchSemanticsNode().boundsInRoot
        val clusterCenter = (headingBounds.top + registerBounds.bottom) / 2f
        val viewportCenter = (viewportBounds.top + viewportBounds.bottom) / 2f
        val centerTolerance = viewportBounds.height * 0.015f

        assertTrue("The login cluster should fit within the available viewport", headingBounds.top >= viewportBounds.top)
        assertTrue("The login cluster should fit within the available viewport", registerBounds.bottom <= viewportBounds.bottom)
        assertTrue(
            "A fitting login cluster should be centered vertically: center=$clusterCenter viewport=$viewportCenter bounds=$viewportBounds/$headingBounds/$registerBounds",
            kotlin.math.abs(clusterCenter - viewportCenter) <= centerTolerance
        )
    }
}
