package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerPaletteContrastTest {
    @Test
    fun primaryAndSupportingTextMeetWcagAaInBothThemes() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            assertContrastAtLeast(palette.ink, palette.canvas, 7.0)
            assertContrastAtLeast(palette.ink, palette.surface, 7.0)
            assertContrastAtLeast(palette.inkMuted, palette.canvas, 4.5)
            assertContrastAtLeast(palette.inkMuted, palette.surface, 4.5)
            assertContrastAtLeast(palette.inkDisabled, palette.canvas, 4.5)
            assertContrastAtLeast(palette.inkDisabled, palette.surface, 4.5)
            assertContrastAtLeast(palette.onBrand, palette.brand, 4.5)
        }
    }

    @Test
    fun semanticStatusTextMeetsWcagAaOnItsTintedBadgeSurface() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            val statuses = listOf(palette.positive, palette.caution, palette.danger, palette.info)
            statuses.forEach { foreground ->
                val tintedSurface = foreground.copy(alpha = 0.11f).compositeOver(palette.surface)
                assertContrastAtLeast(foreground, tintedSurface, 4.5)
            }
        }
    }

    private fun assertContrastAtLeast(foreground: Color, background: Color, minimum: Double) {
        val actual = contrastRatio(foreground, background)
        assertTrue(
            "Contrast ${"%.2f".format(actual)}:1 is below ${minimum}:1 " +
                "for $foreground on $background",
            actual >= minimum
        )
    }

    private fun contrastRatio(first: Color, second: Color): Double {
        val firstLuminance = relativeLuminance(first)
        val secondLuminance = relativeLuminance(second)
        val lighter = maxOf(firstLuminance, secondLuminance)
        val darker = minOf(firstLuminance, secondLuminance)
        return (lighter + 0.05) / (darker + 0.05)
    }

    private fun relativeLuminance(color: Color): Double {
        fun linear(channel: Float): Double =
            if (channel <= 0.04045f) channel / 12.92 else Math.pow(((channel + 0.055) / 1.055).toDouble(), 2.4)

        return 0.2126 * linear(color.red) +
            0.7152 * linear(color.green) +
            0.0722 * linear(color.blue)
    }
}
