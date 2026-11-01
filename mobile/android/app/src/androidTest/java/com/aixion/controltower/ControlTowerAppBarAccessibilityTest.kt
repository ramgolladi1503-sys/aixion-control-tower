package com.aixion.controltower

import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ControlTowerAppBarAccessibilityTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun destinationTitleIsExposedAsAScreenReaderHeading() {
        composeRule.setContent {
            ControlTowerTheme {
                ControlTowerAppBarTitle("Approvals")
            }
        }

        val title = composeRule.onNodeWithText("Approvals")
        assertTrue(
            "The current destination title should be a heading landmark",
            title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
    }
}
