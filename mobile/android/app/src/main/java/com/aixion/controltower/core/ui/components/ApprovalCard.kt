package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun ApprovalCard(
    approval: ApprovalSummary,
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null
) {
    val clickableModifier = if (onClick != null) {
        modifier.clickable { onClick() }
    } else {
        modifier
    }

    TowerPanel(modifier = clickableModifier, elevated = true) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = approval.projectName.uppercase(),
                color = TowerTextMuted,
                fontSize = 10.sp,
                fontWeight = FontWeight.Medium,
                letterSpacing = 1.3.sp
            )
            RiskBadge(risk = approval.risk)
        }
        Spacer(modifier = Modifier.height(12.dp))
        Text(
            text = approval.title,
            color = TowerTextPrimary,
            fontSize = 17.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 22.sp
        )
        Spacer(modifier = Modifier.height(7.dp))
        Text(text = approval.summary, color = TowerTextMuted, fontSize = 13.sp, lineHeight = 18.sp)
        Spacer(modifier = Modifier.height(14.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = approval.status.name, color = TowerTextMuted)
            SourceBadge(approval = approval)
            StatusBadge(label = "${approval.files.size} files", color = TowerTextMuted)
        }
    }
}