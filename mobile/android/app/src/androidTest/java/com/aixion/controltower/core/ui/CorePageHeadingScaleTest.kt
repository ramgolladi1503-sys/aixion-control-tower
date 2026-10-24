package com.aixion.controltower.core.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.feature.auth.AuthFormContent
import com.aixion.controltower.feature.auth.AuthUiState
import com.aixion.controltower.feature.connectors.ConnectorsContent
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class CorePageHeadingScaleTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun signInHeadingWrapsAtLargeFontScaleInsideCompactWidth() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(Modifier.width(320.dp).height(700.dp).testTag("heading-viewport")) {
                    Box(Modifier.padding(horizontal = 20.dp)) {
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

        val title = composeRule.onNodeWithText("Welcome back").assertIsDisplayed()
        val viewport = composeRule.onNodeWithTag("heading-viewport").fetchSemanticsNode().boundsInRoot
        val titleBounds = title.fetchSemanticsNode().boundsInRoot
        assertTrue("Sign-in heading must stay within the compact viewport", titleBounds.right <= viewport.right - 20f)
        assertTrue("Large font scale should wrap the heading vertically", titleBounds.height > 72f)
    }

    @Test
    fun connectHeadingWrapsAtLargeFontScaleInsideCompactWidth() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                Box(Modifier.width(320.dp).height(700.dp).testTag("heading-viewport")) {
                    ConnectorsContent()
                }
            }
        }

        val title = composeRule.onNodeWithText("Connect your coding agent").assertIsDisplayed()
        val viewport = composeRule.onNodeWithTag("heading-viewport").fetchSemanticsNode().boundsInRoot
        val titleBounds = title.fetchSemanticsNode().boundsInRoot
        assertTrue("Connect heading must stay within the compact viewport", titleBounds.right <= viewport.right - 20f)
        assertTrue("Large font scale should wrap the heading vertically", titleBounds.height > 72f)
    }
}
