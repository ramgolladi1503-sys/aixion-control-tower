package com.aixion.controltower.core.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerRadius
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerPalette
import java.time.Instant
import java.time.OffsetDateTime
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
        modifier.clickable(
            role = Role.Button,
            onClickLabel = "Open the full request before deciding",
            onClick = onClick
        )
    } else {
        modifier
    }
    val exactToolRequest = approval.hasExactToolRequestPayload
    val summaryPreview = if (exactToolRequest) {
        "Tool action is hidden in this preview."
    } else {
        approval.summary.ifBlank { "Request summary unavailable." }
    }
    val accessiblePreview = if (exactToolRequest) {
        "Exact tool action preview. Tool action is hidden here."
    } else {
        val boundedSummary = summaryPreview.trim().take(MAX_ACCESSIBLE_PREVIEW_CHARS)
        val previewSuffix = if (summaryPreview.trim().length > MAX_ACCESSIBLE_PREVIEW_CHARS) "…" else ""
        "Request overview preview: $boundedSummary$previewSuffix. This may be incomplete."
    }

    ApprovalCardPanel(
        modifier = clickableModifier.testTag("approval-card"),
        accentColor = palette.brand
    ) {
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
        ) {
            SourceBadge(
                approval = approval,
                modifier = Modifier.testTag("approval-card-source-badge")
            )
            RiskBadge(
                risk = approval.risk,
                readable = true,
                modifier = Modifier.testTag("approval-card-risk-badge")
            )
        }
        Text(
            text = approval.projectName.ifBlank { "Project not provided" },
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier
                .semantics { contentDescription = "Project: ${approval.projectName.ifBlank { "Not provided" }}" }
                .testTag("approval-card-project")
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            text = approval.title,
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.semantics { heading() }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .testTag("approval-card-summary-preview")
                .clip(RoundedCornerShape(TowerRadius.sm))
                .background(palette.surfaceSubtle)
                .padding(horizontal = TowerSpacing.md, vertical = TowerSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
        ) {
            Text(
                text = if (exactToolRequest) "Exact tool action · preview only" else "Request overview · preview only",
                color = palette.inkMuted,
                style = MaterialTheme.typography.labelLarge
            )
            Text(
                text = summaryPreview,
                color = palette.inkMuted,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier
                    .semantics { contentDescription = accessiblePreview }
                    .testTag("approval-card-preview-content")
            )
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        val now = Instant.now()
        val expired = approvalExpiredAt(approval.expiresAt, now)
        val expiryLabel = formatApprovalExpiry(approval.expiresAt, now)
        Text(
            text = expiryLabel,
            color = if (expired || expiryLabel == EXPIRY_UNAVAILABLE) palette.caution else palette.inkMuted,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.semantics {
                if (expired) {
                    contentDescription = "$expiryLabel. Request deadline passed; open details to check its current status."
                }
            }
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
        ) {
            val statusLabel = approval.status.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() }
            StatusBadge(
                label = statusLabel,
                color = approvalStatusColor(approval.status, palette),
                fontSize = MaterialTheme.typography.labelLarge.fontSize,
                uppercase = false,
                modifier = Modifier.testTag("approval-card-status-badge")
            )
            StatusBadge(
                label = when {
                    approval.hasExactToolRequestPayload -> "Tool request"
                    approval.files.size == 1 -> "1 file"
                    else -> "${approval.files.size} files"
                },
                color = palette.inkMuted,
                fontSize = MaterialTheme.typography.labelLarge.fontSize,
                uppercase = false
            )
        }
        if (onClick != null) {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Text(
                text = "Open full request before deciding",
                color = palette.brand,
                style = MaterialTheme.typography.labelLarge,
                textAlign = TextAlign.End,
                modifier = Modifier.fillMaxWidth()
            )
        }
    }
}

internal fun approvalStatusColor(status: ApprovalStatus, palette: TowerPalette): Color =
    when (status) {
        ApprovalStatus.REQUESTED,
        ApprovalStatus.PENDING_REVIEW,
        ApprovalStatus.REVISION_REQUESTED -> palette.caution

        ApprovalStatus.APPROVED,
        ApprovalStatus.APPLIED,
        ApprovalStatus.TESTS_PASSED,
        ApprovalStatus.MERGED -> palette.positive

        ApprovalStatus.DENIED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.BLOCKED,
        ApprovalStatus.FAILED,
        ApprovalStatus.TESTS_FAILED -> palette.danger

        ApprovalStatus.EXECUTING,
        ApprovalStatus.READY_FOR_PR,
        ApprovalStatus.TESTS_RUNNING -> palette.info

        ApprovalStatus.EXPIRED,
        ApprovalStatus.CANCELLED,
        ApprovalStatus.UNKNOWN -> palette.inkMuted
    }

@Composable
private fun ApprovalCardPanel(
    modifier: Modifier,
    accentColor: Color,
    content: @Composable ColumnScope.() -> Unit
) {
    Box(modifier = Modifier.fillMaxWidth()) {
        TowerPanel(modifier = modifier, elevated = true, content = content)
        Box(modifier = Modifier.matchParentSize()) {
            Box(
                modifier = Modifier
                    .align(Alignment.CenterStart)
                    .padding(
                        start = TowerSpacing.xs,
                        top = TowerSpacing.lg,
                        bottom = TowerSpacing.lg
                    )
                    .width(3.dp)
                    .fillMaxHeight()
                    .clip(RoundedCornerShape(2.dp))
                    .background(accentColor)
            )
        }
    }
}

private const val EXPIRY_UNAVAILABLE = "Expiry unavailable"
private const val MAX_ACCESSIBLE_PREVIEW_CHARS = 160

private fun parseApprovalExpiry(expiresAt: String?): Instant? =
    expiresAt?.takeIf(String::isNotBlank)
        ?.let { raw -> runCatching { OffsetDateTime.parse(raw).toInstant() }.getOrNull() }

internal fun approvalExpiredAt(expiresAt: String?, now: Instant): Boolean =
    parseApprovalExpiry(expiresAt)?.let { expiry -> !expiry.isAfter(now) } ?: false

internal fun formatApprovalExpiry(expiresAt: String?, now: Instant = Instant.now()): String {
    val expiry = parseApprovalExpiry(expiresAt) ?: return EXPIRY_UNAVAILABLE
    return runCatching {
        val localFormatter = DateTimeFormatter
            .ofPattern("MMM d, yyyy 'at' h:mm a (XXX)", Locale.getDefault())
            .withZone(ZoneId.systemDefault())
        val time = localFormatter.format(expiry)
        if (!expiry.isAfter(now)) "Expired · $time" else "Expires $time"
    }.getOrDefault(EXPIRY_UNAVAILABLE)
}
