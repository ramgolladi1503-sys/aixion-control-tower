package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerRadius
import com.aixion.controltower.core.ui.theme.TowerSpacing

@Composable
fun DiffBlock(file: FileChangeSummary, modifier: Modifier = Modifier) {
    val palette = LocalTowerPalette.current
    var wrapLongPayloadLines by remember(file.diff) { mutableStateOf(true) }
    val hasHorizontallyScrollablePayload = file.diff
        .lineSequence()
        .any(::containsLongUnbrokenPayloadToken)
    TowerPanel(modifier = modifier, elevated = true) {
        Text(
            text = file.path,
            color = palette.ink,
            fontSize = 15.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 20.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        RiskBadge(risk = file.risk, readable = true)
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        SelectionContainer {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(palette.surfaceRaised, RoundedCornerShape(TowerRadius.md))
                    .border(1.dp, palette.line.copy(alpha = 0.62f), RoundedCornerShape(TowerRadius.md))
                    .padding(TowerSpacing.md)
            ) {
                file.diff.lines().forEachIndexed { lineIndex, line ->
                    val color = when {
                        line.startsWith("+") -> palette.positive
                        line.startsWith("-") -> palette.danger
                        else -> palette.inkMuted
                    }
                    if (containsLongUnbrokenPayloadToken(line) && !wrapLongPayloadLines) {
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .horizontalScroll(rememberScrollState())
                                .testTag("approval-unbroken-payload-scroll-$lineIndex")
                        ) {
                            Text(
                                text = line,
                                color = color,
                                fontSize = 14.sp,
                                fontFamily = FontFamily.Monospace,
                                lineHeight = 21.sp,
                                softWrap = false
                            )
                        }
                    } else {
                        Text(
                            text = line,
                            color = color,
                            fontSize = 14.sp,
                            fontFamily = FontFamily.Monospace,
                            lineHeight = 21.sp,
                            softWrap = true,
                            modifier = if (containsLongUnbrokenPayloadToken(line)) {
                                Modifier.testTag("approval-unbroken-payload-wrapped-$lineIndex")
                            } else {
                                Modifier
                            }
                        )
                    }
                }
            }
        }
        if (hasHorizontallyScrollablePayload) {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .testTag("approval-payload-wrap-toggle")
                    .toggleable(
                        value = wrapLongPayloadLines,
                        role = Role.Switch,
                        onValueChange = { wrapLongPayloadLines = it }
                    )
                    .padding(vertical = TowerSpacing.xs),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "Wrap long lines",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.weight(1f)
                )
                Switch(checked = wrapLongPayloadLines, onCheckedChange = null)
            }
            if (!wrapLongPayloadLines) {
                Text(
                    text = "Swipe horizontally to inspect the full line.",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.testTag("approval-payload-horizontal-scroll-hint")
                )
            }
        }
    }
}

private const val UNBROKEN_PAYLOAD_SCROLL_THRESHOLD = 32

private fun containsLongUnbrokenPayloadToken(line: String): Boolean =
    line.split(Regex("\\s+")).any { token -> token.length > UNBROKEN_PAYLOAD_SCROLL_THRESHOLD }
