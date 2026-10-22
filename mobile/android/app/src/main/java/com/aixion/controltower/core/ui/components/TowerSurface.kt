package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerRadius
import com.aixion.controltower.core.ui.theme.TowerSpacing

@Composable
fun TowerPanel(
    modifier: Modifier = Modifier,
    elevated: Boolean = false,
    content: @Composable ColumnScope.() -> Unit
) {
    val palette = LocalTowerPalette.current
    val shape = RoundedCornerShape(TowerRadius.lg)
    val background = if (elevated) palette.surfaceRaised else palette.surface

    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(background)
            .border(BorderStroke(1.dp, palette.line.copy(alpha = 0.62f)), shape)
            .padding(TowerSpacing.lg),
        content = content
    )
}

@Composable
fun TowerHeroPanel(
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit
) {
    val palette = LocalTowerPalette.current
    val shape = RoundedCornerShape(TowerRadius.xl)
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(
                Brush.verticalGradient(
                    colors = listOf(
                        palette.surfaceRaised.copy(alpha = 0.98f),
                        palette.surface.copy(alpha = 0.94f)
                    )
                )
            )
            .border(BorderStroke(1.dp, palette.line), shape)
            .padding(TowerSpacing.xl),
        content = content
    )
}

@Composable
fun TowerSectionHeader(
    title: String,
    subtitle: String? = null,
    modifier: Modifier = Modifier
) {
    val palette = LocalTowerPalette.current
    Column(modifier = modifier.fillMaxWidth()) {
        Text(
            text = title,
            color = palette.ink,
            fontSize = 22.sp,
            fontWeight = FontWeight.SemiBold
        )
        if (!subtitle.isNullOrBlank()) {
            Text(
                text = subtitle,
                color = palette.inkMuted,
                fontSize = 13.sp,
                lineHeight = 19.sp,
                modifier = Modifier.padding(top = 4.dp)
            )
        }
    }
}

@Composable
fun TowerDivider(modifier: Modifier = Modifier, color: Color? = null) {
    val dividerColor = color ?: LocalTowerPalette.current.line.copy(alpha = 0.62f)
    Box(
        modifier = modifier
            .fillMaxWidth()
            .background(dividerColor)
    )
}
