package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.TowerBorderSoft
import com.aixion.controltower.core.ui.theme.TowerRadius
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerSurfaceRaised
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun DiffBlock(file: FileChangeSummary, modifier: Modifier = Modifier) {
    TowerPanel(modifier = modifier, elevated = true) {
        Text(
            text = file.path,
            color = TowerTextPrimary,
            fontSize = 15.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 20.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        RiskBadge(risk = file.risk)
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(TowerSurfaceRaised, RoundedCornerShape(TowerRadius.md))
                .border(1.dp, TowerBorderSoft, RoundedCornerShape(TowerRadius.md))
                .padding(TowerSpacing.md)
        ) {
            file.diff.lines().forEach { line ->
                val color = when {
                    line.startsWith("+") -> RiskLow
                    line.startsWith("-") -> RiskCritical
                    else -> TowerTextMuted
                }
                val prefix = when {
                    line.startsWith("+") -> "+ "
                    line.startsWith("-") -> "- "
                    else -> "  "
                }
                Text(
                    text = prefix + line.removePrefix("+").removePrefix("-"),
                    color = color,
                    fontSize = 12.sp,
                    fontFamily = FontFamily.Monospace,
                    lineHeight = 18.sp
                )
            }
        }
    }
}
