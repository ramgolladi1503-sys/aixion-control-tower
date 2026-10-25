package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerThemeContractTest {
    @Test
    fun displayHeadingsUseEditorialSerifWhileReadingStylesStaySansSerif() {
        assertEquals(FontFamily.Serif, TowerTypography.displayLarge.fontFamily)
        assertEquals(FontFamily.Serif, TowerTypography.headlineLarge.fontFamily)
        assertEquals(FontFamily.Serif, TowerTypography.headlineMedium.fontFamily)
        assertTrue("Body text should use the platform sans-serif default", TowerTypography.bodyLarge.fontFamily == null)
        assertTrue("Interactive labels should use the platform sans-serif default", TowerTypography.labelLarge.fontFamily == null)
    }

    @Test
    fun semanticTextAndControlsMeetReadableContrastInLightAndDarkThemes() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            listOf(palette.canvas, palette.surface, palette.surfaceRaised).forEach { background ->
                assertContrast(palette.ink, background, 7.0)
                assertContrast(palette.inkMuted, background, 4.5)
            }
            assertContrast(palette.onBrand, palette.brand, 4.5)
        }
    }

    private fun assertContrast(foreground: Color, background: Color, minimum: Double) {
        val ratio = contrastRatio(foreground, background)
        assertTrue("Contrast $ratio is below $minimum", ratio >= minimum)
    }

    private fun contrastRatio(first: Color, second: Color): Double {
        val lighter = maxOf(relativeLuminance(first), relativeLuminance(second))
        val darker = minOf(relativeLuminance(first), relativeLuminance(second))
        return (lighter + 0.05) / (darker + 0.05)
    }

    private fun relativeLuminance(color: Color): Double {
        fun linear(channel: Float): Double {
            val normalized = channel.toDouble()
            return if (normalized <= 0.04045) normalized / 12.92
            else Math.pow((normalized + 0.055) / 1.055, 2.4)
        }

        return 0.2126 * linear(color.red) +
            0.7152 * linear(color.green) +
            0.0722 * linear(color.blue)
    }
}
