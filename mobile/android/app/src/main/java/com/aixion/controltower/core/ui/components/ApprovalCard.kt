package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

@Composable
@OptIn(ExperimentalLayoutApi::class)
fun ApprovalCard(
    approval: ApprovalSummary,
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null
) {
    val palette = LocalTowerPalette.current
    val clickableModifier = if (onClick != null) {
        modifier.clickable(role = Role.Button, onClick = onClick)
    } else {
        modifier
    }

    TowerPanel(modifier = clickableModifier.testTag("approval-card"), elevated = true) {
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
        ) {
            SourceBadge(approval = approval)
            Text(
                text = approval.projectName,
                color = palette.inkMuted,
                fontSize = 14.sp,
                fontWeight = FontWeight.Medium,
                lineHeight = 20.sp,
                modifier = Modifier.weight(1f, fill = false)
            )
            RiskBadge(risk = approval.risk, readable = true)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            text = approval.title,
            color = palette.ink,
            fontSize = 17.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 22.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "Request preview",
            color = palette.inkMuted,
            style = MaterialTheme.typography.labelSmall
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = approval.summary,
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyLarge,
            maxLines = 3,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.testTag("approval-card-summary-preview")
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        val expiryLabel = formatApprovalExpiry(approval.expiresAt)
        Text(
            text = expiryLabel,
            color = if (expiryLabel == EXPIRY_UNAVAILABLE) palette.caution else palette.inkMuted,
            fontSize = 14.sp,
            fontWeight = FontWeight.Medium,
            lineHeight = 20.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
        ) {
            StatusBadge(
                label = approval.status.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() },
                color = palette.inkMuted,
                fontSize = 14.sp,
                uppercase = false
            )
            StatusBadge(label = "${approval.files.size} files", color = palette.inkMuted, fontSize = 14.sp, uppercase = false)
        }
        if (onClick != null) {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Text(
                text = "Review request",
                color = palette.brand,
                fontSize = 14.sp,
                fontWeight = FontWeight.Medium,
                lineHeight = 20.sp,
                textAlign = TextAlign.End,
                modifier = Modifier.fillMaxWidth()
            )
        }
    }
}

private const val EXPIRY_UNAVAILABLE = "Expiry unavailable"

internal fun formatApprovalExpiry(expiresAt: String?): String {
    if (expiresAt.isNullOrBlank()) return EXPIRY_UNAVAILABLE
    return runCatching {
        val localFormatter = DateTimeFormatter
            .ofPattern("MMM d, yyyy 'at' h:mm a (XXX)", Locale.getDefault())
            .withZone(ZoneId.systemDefault())
        "Expires ${localFormatter.format(Instant.parse(expiresAt))}"
    }.getOrDefault(EXPIRY_UNAVAILABLE)
}
