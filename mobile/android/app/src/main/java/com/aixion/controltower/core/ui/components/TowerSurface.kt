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
import com.aixion.controltower.core.ui.theme.TowerBorder
import com.aixion.controltower.core.ui.theme.TowerBorderSoft
import com.aixion.controltower.core.ui.theme.TowerRadius
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerSurfaceRaised
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun TowerPanel(
    modifier: Modifier = Modifier,
    elevated: Boolean = false,
    content: @Composable ColumnScope.() -> Unit
) {
    val shape = RoundedCornerShape(TowerRadius.lg)
    val background = if (elevated) TowerSurfaceRaised else TowerSurface

    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(background)
            .border(BorderStroke(1.dp, TowerBorderSoft), shape)
            .padding(TowerSpacing.lg),
        content = content
    )
}

@Composable
fun TowerHeroPanel(
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit
) {
    val shape = RoundedCornerShape(TowerRadius.xl)
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(
                Brush.verticalGradient(
                    colors = listOf(
                        TowerSurfaceRaised.copy(alpha = 0.98f),
                        TowerSurface.copy(alpha = 0.94f)
                    )
                )
            )
            .border(BorderStroke(1.dp, TowerBorder), shape)
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
    Column(modifier = modifier.fillMaxWidth()) {
        Text(
            text = title,
            color = TowerTextPrimary,
            fontSize = 22.sp,
            fontWeight = FontWeight.SemiBold
        )
        if (!subtitle.isNullOrBlank()) {
            Text(
                text = subtitle,
                color = TowerTextMuted,
                fontSize = 13.sp,
                lineHeight = 19.sp,
                modifier = Modifier.padding(top = 4.dp)
            )
        }
    }
}

@Composable
fun TowerDivider(modifier: Modifier = Modifier, color: Color = TowerBorderSoft) {
    Box(
        modifier = modifier
            .fillMaxWidth()
            .background(color)
    )
}
