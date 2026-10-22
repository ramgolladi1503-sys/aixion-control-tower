package com.aixion.controltower.core.ui.theme

import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/** Four-point spacing scale used across release-one surfaces. */
object TowerSpacing {
    val xs = 4.dp
    val sm = 8.dp
    val md = 12.dp
    val lg = 16.dp
    val xl = 20.dp
    val xxl = 24.dp
    val xxxl = 32.dp
    val xxxxl = 40.dp
    val xxxxxl = 48.dp
}

object TowerRadius {
    val sm = 12.dp
    val md = 16.dp
    val lg = 24.dp
    val xl = 24.dp
}

object TowerLayout {
    val compactContentMaxWidth = 640.dp
    val expandedContentMaxWidth = 720.dp
    val compactBreakpoint = 600.dp
    val expandedBreakpoint = 840.dp
}

@Composable
fun towerScreenHorizontalPadding(): Dp {
    val screenWidth = LocalConfiguration.current.screenWidthDp.dp
    return if (screenWidth >= TowerLayout.compactBreakpoint) 24.dp else 20.dp
}
