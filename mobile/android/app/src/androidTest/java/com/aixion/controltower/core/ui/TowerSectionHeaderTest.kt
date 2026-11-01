package com.aixion.controltower.core.ui

import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class TowerSectionHeaderTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun sectionTitleIsExposedAsHeadingAndSubtitleRemainsReadableContent() {
        composeRule.setContent {
            ControlTowerTheme {
                TowerSectionHeader(
                    title = "Needs your decision",
                    subtitle = "Review the exact request before responding."
                )
            }
        }

        val title = composeRule.onNodeWithText("Needs your decision")
        title.assertExists()
        assertTrue(
            "Shared section titles should be exposed as headings to accessibility services",
            title.fetchSemanticsNode().config.contains(SemanticsProperties.Heading)
        )
        composeRule.onNodeWithText("Review the exact request before responding.").assertExists()
    }
}
