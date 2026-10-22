package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskHigh
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerTextMuted

@Composable
fun RiskBadge(risk: RiskLevel, modifier: Modifier = Modifier, readable: Boolean = false) {
    val color = when (risk) {
        RiskLevel.LOW -> RiskLow
        RiskLevel.MEDIUM -> RiskMedium
        RiskLevel.HIGH -> RiskHigh
        RiskLevel.CRITICAL -> RiskCritical
        RiskLevel.BLOCKED -> RiskBlocked
        RiskLevel.UNKNOWN -> RiskCritical
    }

    StatusBadge(
        label = if (readable) risk.name.toSentenceCase() else risk.name,
        color = color,
        modifier = modifier,
        fontSize = if (readable) 14.sp else 10.sp,
        uppercase = !readable
    )
}

@Composable
fun StatusBadge(
    label: String,
    color: Color,
    modifier: Modifier = Modifier,
    fontSize: TextUnit = 10.sp,
    uppercase: Boolean = true
) {
    Box(
        modifier = modifier
            .background(color.copy(alpha = 0.11f), RoundedCornerShape(999.dp))
            .border(BorderStroke(1.dp, color.copy(alpha = 0.34f)), RoundedCornerShape(999.dp))
            .padding(horizontal = 10.dp, vertical = 5.dp)
    ) {
        Text(
            text = if (uppercase) label.uppercase() else label,
            color = color,
            fontSize = fontSize,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = if (uppercase) 0.8.sp else 0.sp,
            lineHeight = (fontSize.value * 1.35f).sp
        )
    }
}

private fun String.toSentenceCase(): String =
    lowercase().replace('_', ' ').replaceFirstChar { char -> char.titlecase() }

@Composable
fun SourceBadge(approval: ApprovalSummary, modifier: Modifier = Modifier) {
    val color = when {
        approval.verifiedSource -> RiskLow
        approval.sourceProvider == "MANUAL" -> TowerTextMuted
        else -> RiskMedium
    }
    val label = when {
        approval.verifiedSource -> "Verified: ${approval.sourceLabel}"
        approval.sourceProvider == "MANUAL" -> "Manual source"
        else -> "Unverified: ${approval.sourceLabel}"
    }
    StatusBadge(label = label, color = color, modifier = modifier, fontSize = 14.sp, uppercase = false)
}
