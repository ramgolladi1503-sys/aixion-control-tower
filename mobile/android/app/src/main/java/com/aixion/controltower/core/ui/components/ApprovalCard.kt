package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
@OptIn(ExperimentalLayoutApi::class)
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
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            Text(
                text = approval.projectName,
                color = TowerTextMuted,
                fontSize = 14.sp,
                fontWeight = FontWeight.Medium,
                lineHeight = 20.sp,
                modifier = Modifier.weight(1f, fill = false)
            )
            RiskBadge(risk = approval.risk, readable = true)
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
        Text(text = approval.summary, color = TowerTextMuted, fontSize = 14.sp, lineHeight = 21.sp)
        Spacer(modifier = Modifier.height(14.dp))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            StatusBadge(
                label = approval.status.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() },
                color = TowerTextMuted,
                fontSize = 14.sp,
                uppercase = false
            )
            SourceBadge(approval = approval)
            StatusBadge(label = "${approval.files.size} files", color = TowerTextMuted, fontSize = 14.sp, uppercase = false)
        }
    }
}
