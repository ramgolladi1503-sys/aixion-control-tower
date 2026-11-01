package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class StatusCardTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun statusCardKeepsScaledTextVisibleAndCardActionAvailable() {
        var clicked = false
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    Box(modifier = Modifier.width(180.dp)) {
                        StatusCard(
                            title = "Pending approvals",
                            value = "7 waiting",
                            subtitle = "Review requests from your connected agent sessions.",
                            accent = Color(0xFF155EEF),
                            onClick = { clicked = true }
                        )
                    }
                }
            }
        }

        val title = composeRule.onNodeWithText("PENDING APPROVALS")
        val value = composeRule.onNodeWithText("7 waiting")
        val subtitle = composeRule.onNodeWithText(
            "Review requests from your connected agent sessions."
        )
        title.assertIsDisplayed()
        value.assertIsDisplayed()
        subtitle.assertIsDisplayed()

        listOf(title, value, subtitle).forEach { node ->
            val bounds = node.fetchSemanticsNode().boundsInRoot
            assertTrue("Scaled text must have non-zero visible bounds", bounds.width > 0f && bounds.height > 0f)
        }

        value.performClick()
        composeRule.runOnIdle { assertEquals(true, clicked) }
    }
}
