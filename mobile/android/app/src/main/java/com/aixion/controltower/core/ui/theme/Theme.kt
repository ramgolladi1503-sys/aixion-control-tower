package com.aixion.controltower.core.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable

private val DarkColors = darkColorScheme(
    primary = TowerAccent,
    background = TowerBackground,
    surface = TowerSurface,
    onPrimary = TowerBackground,
    onBackground = TowerTextPrimary,
    onSurface = TowerTextPrimary
)

@Composable
fun ControlTowerTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = DarkColors,
        typography = MaterialTheme.typography,
        content = content
    )
}
