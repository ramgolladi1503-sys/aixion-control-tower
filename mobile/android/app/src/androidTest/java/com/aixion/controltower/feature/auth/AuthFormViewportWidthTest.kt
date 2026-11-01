package com.aixion.controltower.feature.auth

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class AuthFormViewportWidthTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun signInFormIsCenteredAndCappedOnWideViewport() {
        composeRule.setContent {
            ControlTowerTheme {
                Box(
                    modifier = Modifier
                        .width(900.dp)
                        .height(800.dp)
                        .testTag("auth-wide-viewport")
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

        val viewport = composeRule.onNodeWithTag("auth-wide-viewport").fetchSemanticsNode().boundsInRoot
        val form = composeRule.onNodeWithTag("auth-form-content").fetchSemanticsNode().boundsInRoot
        val density = composeRule.density

        assertTrue("Sign-in form should stay within the 640dp content width", form.width <= 640f * density + 1f)
        assertEquals("Sign-in form should remain horizontally centered", viewport.center.x, form.center.x, 1f)
    }
}
