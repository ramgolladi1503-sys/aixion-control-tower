package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun ForgedLogoMark(
    modifier: Modifier = Modifier,
    size: Dp = 42.dp,
    color: Color = TowerTextPrimary
) {
    Box(modifier = modifier.size(size)) {
        Canvas(modifier = Modifier.size(size)) {
            val scale = this.size.minDimension / 120f
            fun p(x: Float, y: Float) = Offset(x * scale, y * scale)
            val strokeWidth = 6f * scale
            val stroke = Stroke(width = strokeWidth, cap = StrokeCap.Round, join = StrokeJoin.Round)

            // Approved Woven / Forged Network direction:
            // two interlocked loops, one center approval point, and a bridge line.
            drawLine(color = color, start = p(27f, 60f), end = p(93f, 60f), strokeWidth = 4f * scale, cap = StrokeCap.Round)

            drawLine(color = color, start = p(28f, 60f), end = p(45f, 38f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
            drawLine(color = color, start = p(45f, 38f), end = p(60f, 60f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
            drawLine(color = color, start = p(60f, 60f), end = p(45f, 82f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
            drawLine(color = color, start = p(45f, 82f), end = p(28f, 60f), strokeWidth = strokeWidth, cap = StrokeCap.Round)

            drawLine(color = color, start = p(60f, 60f), end = p(75f, 38f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
            drawLine(color = color, start = p(75f, 38f), end = p(92f, 60f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
            drawLine(color = color, start = p(92f, 60f), end = p(75f, 82f), strokeWidth = strokeWidth, cap = StrokeCap.Round)
            drawLine(color = color, start = p(75f, 82f), end = p(60f, 60f), strokeWidth = strokeWidth, cap = StrokeCap.Round)

            drawCircle(color = color, radius = 7f * scale, center = p(60f, 60f))
            drawCircle(color = color.copy(alpha = 0.78f), radius = 4f * scale, center = p(28f, 60f))
            drawCircle(color = color.copy(alpha = 0.78f), radius = 4f * scale, center = p(92f, 60f))
        }
    }
}
