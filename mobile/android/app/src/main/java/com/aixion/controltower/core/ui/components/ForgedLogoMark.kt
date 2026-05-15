package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
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
            val strokeWidth = 7f * scale
            val stroke = Stroke(width = strokeWidth, cap = StrokeCap.Square, join = StrokeJoin.Miter)

            fun drawPolyline(points: List<Offset>) {
                val path = Path().apply {
                    moveTo(points.first().x, points.first().y)
                    points.drop(1).forEach { lineTo(it.x, it.y) }
                }
                drawPath(path = path, color = color, style = stroke)
            }

            drawPolyline(listOf(p(27f, 74f), p(44f, 91f), p(61f, 74f), p(77f, 91f), p(94f, 74f)))
            drawPolyline(listOf(p(27f, 46f), p(44f, 29f), p(60f, 45f), p(77f, 29f), p(94f, 46f)))
            drawPolyline(listOf(p(27f, 46f), p(44f, 63f), p(27f, 80f)))
            drawPolyline(listOf(p(94f, 46f), p(77f, 63f), p(94f, 80f)))
            drawPolyline(listOf(p(44f, 29f), p(77f, 91f)))
            drawPolyline(listOf(p(77f, 29f), p(44f, 91f)))

            fun filledPath(points: List<Offset>) {
                val path = Path().apply {
                    moveTo(points.first().x, points.first().y)
                    points.drop(1).forEach { lineTo(it.x, it.y) }
                    close()
                }
                drawPath(path = path, color = color)
            }

            filledPath(listOf(p(58f, 19f), p(58f, 38f), p(69f, 27f), p(69f, 8f)))
            filledPath(listOf(p(28f, 80f), p(17f, 91f), p(31f, 91f), p(39f, 83f)))
            filledPath(listOf(p(92f, 80f), p(103f, 91f), p(89f, 91f), p(81f, 83f)))
        }
    }
}
