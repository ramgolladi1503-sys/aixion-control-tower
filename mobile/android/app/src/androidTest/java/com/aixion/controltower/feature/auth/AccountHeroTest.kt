package com.aixion.controltower.feature.auth

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class AccountHeroTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun accountHeroUsesFullWidthForHeadlineAtLargeTextAndCompactWidth() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    Box(modifier = Modifier.width(240.dp)) {
                        AccountHero(state = AuthUiState(authenticated = true))
                    }
                }
            }
        }

        val title = composeRule.onNodeWithText("Your account")
        title.assertExists()
        composeRule.onNodeWithTag("account-hero-logo").assertDoesNotExist()
        val bounds = title.fetchSemanticsNode().boundsInRoot
        assertTrue("The headline should remain inside the compact hero", bounds.right <= 240f)
        assertTrue("The headline should wrap naturally at 200% font scale", bounds.height > 56f)
        assertTrue(
            "The account title should remain a screen-reader heading",
            title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
    }

    @Test
    fun accountHeroRetainsBrandMarkAtRegularWidthAndTextScale() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 1f)) {
                    Box(modifier = Modifier.width(500.dp)) {
                        AccountHero(state = AuthUiState(authenticated = true))
                    }
                }
            }
        }

        composeRule.onNodeWithTag("account-hero-logo").assertExists()
    }
}
