package com.aixion.controltower.core.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.material3.Typography
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

private fun TowerPalette.toColorScheme(darkTheme: Boolean) = if (darkTheme) {
    darkColorScheme(
        primary = brand,
        onPrimary = onBrand,
        secondary = info,
        onSecondary = canvas,
        tertiary = caution,
        onTertiary = canvas,
        error = danger,
        onError = canvas,
        background = canvas,
        onBackground = ink,
        surface = surface,
        onSurface = ink,
        surfaceVariant = surfaceSubtle,
        onSurfaceVariant = inkMuted,
        outline = line,
        outlineVariant = line
    )
} else {
    lightColorScheme(
        primary = brand,
        onPrimary = onBrand,
        secondary = info,
        onSecondary = surfaceRaised,
        tertiary = caution,
        onTertiary = surfaceRaised,
        error = danger,
        onError = surfaceRaised,
        background = canvas,
        onBackground = ink,
        surface = surface,
        onSurface = ink,
        surfaceVariant = surfaceSubtle,
        onSurfaceVariant = inkMuted,
        outline = line,
        outlineVariant = line
    )
}

internal val TowerTypography = Typography(
    displayLarge = TextStyle(fontSize = 28.sp, lineHeight = 34.sp, fontWeight = FontWeight.SemiBold),
    headlineLarge = TextStyle(fontSize = 28.sp, lineHeight = 34.sp, fontWeight = FontWeight.SemiBold),
    headlineMedium = TextStyle(fontSize = 24.sp, lineHeight = 30.sp, fontWeight = FontWeight.SemiBold),
    headlineSmall = TextStyle(fontSize = 20.sp, lineHeight = 26.sp, fontWeight = FontWeight.SemiBold),
    titleLarge = TextStyle(fontSize = 20.sp, lineHeight = 26.sp, fontWeight = FontWeight.SemiBold),
    titleMedium = TextStyle(fontSize = 16.sp, lineHeight = 24.sp, fontWeight = FontWeight.Medium),
    titleSmall = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.Medium),
    bodyLarge = TextStyle(fontSize = 16.sp, lineHeight = 24.sp),
    bodyMedium = TextStyle(fontSize = 14.sp, lineHeight = 21.sp),
    bodySmall = TextStyle(fontSize = 14.sp, lineHeight = 20.sp),
    labelLarge = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.Medium),
    labelMedium = TextStyle(fontSize = 14.sp, lineHeight = 20.sp, fontWeight = FontWeight.Medium),
    labelSmall = TextStyle(fontSize = 14.sp, lineHeight = 18.sp, fontWeight = FontWeight.Medium)
)

val TowerCodeTextStyle = TextStyle(
    fontFamily = FontFamily.Monospace,
    fontSize = 14.sp,
    lineHeight = 21.sp
)

@Composable
fun ControlTowerTheme(content: @Composable () -> Unit) {
    val darkTheme = isSystemInDarkTheme()
    val palette = if (darkTheme) DarkTowerPalette else LightTowerPalette
    CompositionLocalProvider(LocalTowerPalette provides palette) {
        MaterialTheme(
            colorScheme = palette.toColorScheme(darkTheme),
            typography = TowerTypography,
            content = content
        )
    }
}
