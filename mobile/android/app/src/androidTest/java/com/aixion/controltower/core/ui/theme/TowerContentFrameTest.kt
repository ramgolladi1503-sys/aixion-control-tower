package com.aixion.controltower.core.ui.theme

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.unit.dp
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class TowerContentFrameTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun compactViewportKeepsContentFullWidth() {
        composeRule.setContent {
            Box(modifier = Modifier.width(240.dp).height(500.dp)) {
                TowerContentFrame {
                    Box(modifier = Modifier.fillMaxSize().testTag("framed-content"))
                }
            }
        }

        val bounds = composeRule.onNodeWithTag("framed-content").fetchSemanticsNode().boundsInRoot
        val expectedWidthPx = with(composeRule.density) { 240.dp.toPx() }
        composeRule.runOnIdle {
            assertEquals("Compact content should start at the viewport edge", 0f, bounds.left, 1f)
            assertEquals("Compact content should fill the available width", expectedWidthPx, bounds.width, 1f)
        }
    }
}
