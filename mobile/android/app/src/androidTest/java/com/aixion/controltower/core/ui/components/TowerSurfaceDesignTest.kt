package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class TowerSurfaceDesignTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun compactHeroReducesPaddingWithoutChangingItsCopy() {
        composeRule.setContent {
            ControlTowerTheme {
                Column(modifier = Modifier.width(360.dp)) {
                    TowerHeroPanel(modifier = Modifier.testTag("standard-hero")) {
                        Text("Needs your decision", modifier = Modifier.testTag("standard-hero-copy"))
                    }
                    TowerHeroPanel(compact = true, modifier = Modifier.testTag("compact-hero")) {
                        Text("Needs your decision", modifier = Modifier.testTag("compact-hero-copy"))
                    }
                }
            }
        }

        val standard = composeRule.onNodeWithTag("standard-hero").fetchSemanticsNode().boundsInRoot
        val compact = composeRule.onNodeWithTag("compact-hero").fetchSemanticsNode().boundsInRoot
        val standardCopy = composeRule.onNodeWithTag("standard-hero-copy").fetchSemanticsNode().boundsInRoot
        val compactCopy = composeRule.onNodeWithTag("compact-hero-copy").fetchSemanticsNode().boundsInRoot

        assertTrue("Compact hero keeps the same message", standardCopy.height == compactCopy.height)
        assertTrue("Compact hero removes excess vertical padding", compact.height < standard.height)
    }

    @Test
    fun warmSurfacesContainWrappedLargeTextAtCompactWidth() {
        composeRule.setContent {
            ControlTowerTheme {
                CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                    Box(modifier = Modifier.width(320.dp).testTag("surface-design-viewport")) {
                        Column(
                            modifier = Modifier.fillMaxWidth(),
                            verticalArrangement = Arrangement.spacedBy(16.dp)
                        ) {
                            TowerHeroPanel(modifier = Modifier.testTag("surface-design-hero")) {
                                Text(
                                    text = "Review the exact request before continuing the work on your computer.",
                                    modifier = Modifier.testTag("surface-design-hero-copy"),
                                    color = LocalTowerPalette.current.ink,
                                    style = MaterialTheme.typography.bodyLarge
                                )
                            }
                            TowerPanel(
                                modifier = Modifier.testTag("surface-design-card"),
                                elevated = true
                            ) {
                                Text(
                                    text = "Antigravity · project connector · awaiting your decision",
                                    modifier = Modifier.testTag("surface-design-card-copy"),
                                    color = LocalTowerPalette.current.ink,
                                    style = MaterialTheme.typography.bodyLarge
                                )
                            }
                            Spacer(modifier = Modifier.height(1.dp))
                        }
                    }
                }
            }
        }

        val viewport = composeRule.onNodeWithTag("surface-design-viewport")
            .fetchSemanticsNode().boundsInRoot
        val hero = composeRule.onNodeWithTag("surface-design-hero")
            .fetchSemanticsNode().boundsInRoot
        val heroCopy = composeRule.onNodeWithTag("surface-design-hero-copy")
            .fetchSemanticsNode().boundsInRoot
        val card = composeRule.onNodeWithTag("surface-design-card")
            .fetchSemanticsNode().boundsInRoot
        val cardCopy = composeRule.onNodeWithTag("surface-design-card-copy")
            .fetchSemanticsNode().boundsInRoot

        assertTrue(
            "Hero surface should fit the compact viewport",
            hero.left >= viewport.left && hero.right <= viewport.right
        )
        assertTrue(
            "Elevated card should fit the compact viewport",
            card.left >= viewport.left && card.right <= viewport.right
        )
        assertTrue(
            "Hero copy should stay inside its padded surface",
            heroCopy.left > hero.left && heroCopy.right < hero.right
        )
        assertTrue(
            "Card copy should stay inside its padded surface",
            cardCopy.left > card.left && cardCopy.right < card.right
        )
        assertTrue("Hero copy should wrap at 200% font scale", heroCopy.height > 48f)
        assertTrue("Card copy should wrap at 200% font scale", cardCopy.height > 48f)
    }
}
