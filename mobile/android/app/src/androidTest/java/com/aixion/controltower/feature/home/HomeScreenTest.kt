package com.aixion.controltower.feature.home

import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.Density
import java.time.Instant
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class HomeScreenTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun lastSuccessfulSyncRemainsReadableAtLargeFontScale() {
        composeRule.setContent {
            CompositionLocalProvider(LocalDensity provides Density(density = 1f, fontScale = 2f)) {
                HomeSyncTimestamp(Instant.parse("2026-10-22T10:00:00Z"))
            }
        }

        val timestamp = composeRule.onNodeWithText("Last successful sync", substring = true)
        timestamp.assertExists()
        val timestampBounds = timestamp.fetchSemanticsNode().boundsInRoot
        assertTrue("Timestamp should have visible layout bounds", timestampBounds.width > 0f && timestampBounds.height > 0f)
        assertTrue("Timestamp should scale to at least 40dp high", timestampBounds.height >= 40f)
    }
}
