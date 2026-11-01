package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test

class TowerPaletteContrastTest {
    @Test
    fun semanticPaletteMatchesTheReleaseOneWarmPaperAndOliveTokens() {
        assertEquals(Color(0xFFF4F2EC), LightTowerPalette.canvas)
        assertEquals(Color(0xFFFBFAF6), LightTowerPalette.surface)
        assertEquals(Color(0xFF53664D), LightTowerPalette.brand)
        assertEquals(Color(0xFF171A16), DarkTowerPalette.canvas)
        assertEquals(Color(0xFF20241F), DarkTowerPalette.surface)
        assertEquals(Color(0xFFA9C19B), DarkTowerPalette.brand)
    }

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

    @Test
    fun semanticStateColorsRemainReadableOnLightAndDarkCanvases() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            listOf(palette.positive, palette.caution, palette.danger, palette.info).forEach { foreground ->
                assertContrastAtLeast(foreground, palette.canvas, 4.5)
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
            if (channel <= 0.04045f) channel / 12.92 else Math.pow(((channel + 0.055f) / 1.055f).toDouble(), 2.4)

        return 0.2126 * linear(color.red) +
            0.7152 * linear(color.green) +
            0.0722 * linear(color.blue)
    }
}
