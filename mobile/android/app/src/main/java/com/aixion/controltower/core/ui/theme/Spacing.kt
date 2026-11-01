package com.aixion.controltower.core.ui.theme

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
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
    val md = 20.dp
    val lg = 28.dp
    val xl = 32.dp
}

object TowerLayout {
    val authFormMaxWidth = 480.dp
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

/** Returns the readable content width for the available screen size. */
internal fun towerContentMaxWidth(availableWidth: Dp): Dp =
    if (availableWidth >= TowerLayout.expandedBreakpoint) {
        TowerLayout.expandedContentMaxWidth
    } else {
        TowerLayout.compactContentMaxWidth
    }

/** Centers release-one content and prevents long reading surfaces from stretching on tablets. */
@Composable
fun TowerContentFrame(
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit
) {
    BoxWithConstraints(
        modifier = modifier.fillMaxSize(),
        contentAlignment = Alignment.TopCenter
    ) {
        Box(
            modifier = Modifier
                .width(minOf(maxWidth, towerContentMaxWidth(maxWidth)))
                .fillMaxHeight()
        ) {
            content()
        }
    }
}
