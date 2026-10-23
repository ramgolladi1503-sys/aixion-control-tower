package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerPaletteContrastTest {
    @Test
    fun primaryAndSemanticTextColorsMeetWcagAaOnTheirSurfaces() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            assertAaContrast("primary action", palette.onBrand, palette.brand)
            assertAaContrast("primary text", palette.ink, palette.canvas)
            assertAaContrast("secondary text", palette.inkMuted, palette.canvas)
            assertAaContrast("positive status", palette.positive, palette.canvas)
            assertAaContrast("caution status", palette.caution, palette.canvas)
            assertAaContrast("danger status", palette.danger, palette.canvas)

            listOf(palette.positive, palette.caution, palette.danger).forEach { statusColor ->
                val badgeSurface = statusColor.copy(alpha = 0.11f).compositeOver(palette.surfaceRaised)
                assertAaContrast("status badge", statusColor, badgeSurface)
            }
        }
    }

    private fun assertAaContrast(label: String, foreground: Color, background: Color) {
        val ratio = contrastRatio(foreground, background)
        assertTrue("$label contrast should meet WCAG AA (4.5:1), got ${"%.2f".format(ratio)}", ratio >= 4.5)
    }

    private fun contrastRatio(foreground: Color, background: Color): Double {
        val foregroundLuminance = luminance(foreground)
        val backgroundLuminance = luminance(background)
        val lighter = maxOf(foregroundLuminance, backgroundLuminance)
        val darker = minOf(foregroundLuminance, backgroundLuminance)
        return (lighter + 0.05) / (darker + 0.05)
    }

    private fun luminance(color: Color): Double {
        fun linear(channel: Float): Double {
            val value = channel.toDouble()
            return if (value <= 0.04045) value / 12.92 else Math.pow((value + 0.055) / 1.055, 2.4)
        }

        return 0.2126 * linear(color.red) + 0.7152 * linear(color.green) + 0.0722 * linear(color.blue)
    }
}
