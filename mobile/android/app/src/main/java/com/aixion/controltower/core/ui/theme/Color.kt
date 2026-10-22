package com.aixion.controltower.core.ui.theme

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/** Semantic colors for the quiet, warm Control Tower visual system. */
internal data class TowerPalette(
    val canvas: Color,
    val surface: Color,
    val surfaceRaised: Color,
    val surfaceSubtle: Color,
    val ink: Color,
    val inkMuted: Color,
    val inkDisabled: Color,
    val line: Color,
    val brand: Color,
    val onBrand: Color,
    val positive: Color,
    val caution: Color,
    val danger: Color,
    val info: Color
)

internal val LightTowerPalette = TowerPalette(
    canvas = Color(0xFFF4F2EC),
    surface = Color(0xFFFBFAF6),
    surfaceRaised = Color(0xFFFFFFFF),
    surfaceSubtle = Color(0xFFEAE8E0),
    ink = Color(0xFF20241F),
    inkMuted = Color(0xFF596057),
    // Keep disabled labels readable while pairing them with a state label/icon.
    inkDisabled = Color(0xFF656B61),
    line = Color(0xFFD4D5CC),
    brand = Color(0xFF53664D),
    onBrand = Color(0xFFFFFFFF),
    positive = Color(0xFF286348),
    caution = Color(0xFF805600),
    danger = Color(0xFFA23F35),
    info = Color(0xFF3B6178)
)

internal val LocalTowerPalette = staticCompositionLocalOf { LightTowerPalette }

/** Stable legacy tokens retain their value-level API for non-Compose status mappers. */
val TowerBackground = LightTowerPalette.canvas
val TowerSurface = LightTowerPalette.surface
val TowerSurfaceRaised = LightTowerPalette.surfaceRaised
val TowerSurfaceStrong = LightTowerPalette.surfaceSubtle
val TowerBorder = LightTowerPalette.line
val TowerBorderSoft = LightTowerPalette.line.copy(alpha = 0.62f)
val TowerTextPrimary = LightTowerPalette.ink
val TowerTextMuted = LightTowerPalette.inkMuted
val TowerTextSoft = LightTowerPalette.inkDisabled
val TowerAccent = LightTowerPalette.brand
val TowerAccentMuted = LightTowerPalette.inkMuted
val RiskLow = LightTowerPalette.positive
val RiskMedium = LightTowerPalette.caution
val RiskHigh = LightTowerPalette.danger
val RiskCritical = LightTowerPalette.danger
val RiskBlocked = LightTowerPalette.danger
