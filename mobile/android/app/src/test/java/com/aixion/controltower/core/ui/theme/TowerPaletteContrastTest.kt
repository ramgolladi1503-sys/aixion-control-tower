package com.aixion.controltower.core.ui.theme

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.compositeOver
import org.junit.Assert.assertTrue
import org.junit.Test

class TowerPaletteContrastTest {
    @Test
    fun controlOutlineTokensMeetThreeToOneAcrossEveryThemeSurface() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            val surfaces = listOf(
                "canvas" to palette.canvas,
                "surface" to palette.surface,
                "raised surface" to palette.surfaceRaised,
                "subtle surface" to palette.surfaceSubtle
            )

            surfaces.forEach { (surfaceName, background) ->
                val ratio = contrastRatio(palette.line, background)
                assertTrue(
                    "Control outline on $surfaceName should meet 3:1 non-text contrast, got ${"%.2f".format(ratio)}",
                    ratio >= 3.0
                )
            }
        }
    }

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

    @Test
    fun semanticTextTokensMeetWcagAaAcrossEveryThemeSurface() {
        listOf(LightTowerPalette, DarkTowerPalette).forEach { palette ->
            val surfaces = listOf(
                "canvas" to palette.canvas,
                "surface" to palette.surface,
                "raised surface" to palette.surfaceRaised,
                "subtle surface" to palette.surfaceSubtle
            )
            val bodyAndSupportingText = listOf(
                "body text" to palette.ink,
                "muted text" to palette.inkMuted,
                "disabled text" to palette.inkDisabled
            )
            val semanticLabels = listOf(
                "brand text" to palette.brand,
                "positive text" to palette.positive,
                "caution text" to palette.caution,
                "danger text" to palette.danger,
                "info text" to palette.info
            )

            bodyAndSupportingText.forEach { (label, foreground) ->
                surfaces.forEach { (surfaceName, background) ->
                    assertAaContrast("$label on $surfaceName", foreground, background)
                }
            }
            semanticLabels.forEach { (label, foreground) ->
                surfaces.forEach { (surfaceName, background) ->
                    assertAaContrast("$label on $surfaceName", foreground, background)
                }
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
