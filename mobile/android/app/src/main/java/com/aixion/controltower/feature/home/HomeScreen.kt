package com.aixion.controltower.feature.home

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.ui.ForegroundRefreshEffect
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding
import java.text.DateFormat
import java.time.Instant
import java.util.Date

@Composable
fun HomeScreen(
    viewModel: HomeViewModel = viewModel(),
    onApprovalSelected: (ApprovalSummary) -> Unit = {},
    onOpenConnect: () -> Unit = {},
    onOpenApprovals: () -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ForegroundRefreshEffect(onRefresh = viewModel::refreshInForeground)

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            HomeHeader(
                loading = state.loading,
                refreshing = state.refreshing,
                hasError = state.hasError,
                hasSuccessfulSync = state.hasSuccessfulSync,
                lastSuccessfulSyncAt = state.lastSuccessfulSyncAt
            )
        }

        item {
            TowerHeroPanel {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.Top
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        StatusBadge(
                            label = when {
                                state.hasError -> "BACKEND ERROR"
                                !state.hasSuccessfulSync && !state.loading && !state.refreshing -> "NOT VERIFIED"
                                state.unsupportedApprovals.isNotEmpty() -> "UNSUPPORTED APPROVAL"
                                state.actionRequiredCount > 0 -> "APPROVAL QUEUE"
                                state.refreshing -> "SYNCING"
                                else -> "COMMAND STATE"
                            },
                            color = when {
                                state.hasError -> RiskCritical
                                state.unsupportedApprovals.isNotEmpty() -> RiskCritical
                                state.actionRequiredCount > 0 -> RiskMedium
                                state.refreshing -> RiskMedium
                                else -> RiskLow
                            }
                        )
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        Text(
                            text = homeHeroTitle(state),
                            color = TowerTextPrimary,
                            fontSize = 28.sp,
                            fontWeight = FontWeight.SemiBold,
                            lineHeight = 33.sp
                        )
                        Spacer(modifier = Modifier.height(TowerSpacing.sm))
                        Text(
                            text = homeHeroSubtitle(state),
                            color = TowerTextMuted,
                            fontSize = 14.sp,
                            lineHeight = 20.sp
                        )
                    }
                    ForgedLogoMark(size = 56.dp, color = TowerTextPrimary.copy(alpha = 0.82f))
                }
            }
        }

        item {
            TowerPanel(elevated = false) {
                Text(
                    text = "Agent connections",
                    color = TowerTextPrimary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.SemiBold
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "Set up or review a local hook for Antigravity or Codex. The computer and agent need to stay available while an approval is waiting.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Button(onClick = onOpenConnect, modifier = Modifier.fillMaxWidth()) {
                    Text("Open Connect")
                }
            }
        }

        state.errorMessage?.let { message ->
            item {
                ErrorPanel(
                    title = "Live backend data unavailable",
                    body = message,
                    onRetry = viewModel::refresh,
                    retryEnabled = !state.loading
                )
            }
        }

        if (state.hasError) {
            item {
                EmptyHomePanel(
                    title = "No fallback approvals loaded",
                    body = "This is intentional. Authenticated product screens now wait for real backend data instead of silently showing demo data. Use Retry after the backend is reachable."
                )
            }
        } else if (!state.refreshing && state.hasSuccessfulSync && state.actionRequiredApprovals.isNotEmpty()) {
            item {
                HomeApprovalPreview(
                    approvals = state.actionRequiredApprovals,
                    onApprovalSelected = onApprovalSelected,
                    onOpenApprovals = onOpenApprovals
                )
            }
        } else if (!state.refreshing && state.hasSuccessfulSync && state.unsupportedApprovals.isEmpty()) {
            item {
                EmptyHomePanel(
                    title = "No approvals waiting",
                    body = "The approval queue is clear. New requests will appear here when an agent needs your decision."
                )
            }
        }

        if (!state.hasError && !state.refreshing && state.hasSuccessfulSync && state.unsupportedApprovals.isNotEmpty()) {
            item {
                ErrorPanel(
                    title = "Approval data is not recognized",
                    body = "Update the app or backend before deciding. These requests remain visible but cannot be approved.",
                    onRetry = viewModel::refresh,
                    retryEnabled = !state.loading
                )
            }
            item {
                TowerSectionHeader(
                    title = "Unsupported approval data",
                    subtitle = "Decision controls are disabled for unrecognized status or risk values."
                )
            }
            items(state.unsupportedApprovals) { approval ->
                ApprovalCard(approval = approval, onClick = { onApprovalSelected(approval) })
            }
        }

        item {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
        }
    }
}

@Composable
internal fun HomeApprovalPreview(
    approvals: List<ApprovalSummary>,
    onApprovalSelected: (ApprovalSummary) -> Unit,
    onOpenApprovals: () -> Unit
) {
    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)) {
        TowerSectionHeader(
            title = "Needs your decision",
            subtitle = "Review the exact request before approving or rejecting it."
        )
        approvals.take(3).forEach { approval ->
            ApprovalCard(approval = approval, onClick = { onApprovalSelected(approval) })
        }
        TextButton(onClick = onOpenApprovals, modifier = Modifier.fillMaxWidth()) {
            Text("See all approvals")
        }
    }
}

@Composable
private fun HomeHeader(
    loading: Boolean,
    refreshing: Boolean,
    hasError: Boolean,
    hasSuccessfulSync: Boolean,
    lastSuccessfulSyncAt: Instant?
) {
    Column {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text(
                    text = "AIXION",
                    color = TowerTextPrimary,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold,
                    letterSpacing = 2.2.sp
                )
                Text(
                    text = "CONTROL TOWER",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Medium,
                    letterSpacing = 1.8.sp
                )
            }
            StatusBadge(
                label = homeBackendStatusLabel(
                    loading = loading,
                    refreshing = refreshing,
                    hasError = hasError,
                    hasSuccessfulSync = hasSuccessfulSync
                ),
                color = when {
                    hasError -> RiskCritical
                    loading || refreshing || !hasSuccessfulSync -> RiskMedium
                    else -> RiskLow
                }
            )
        }
        lastSuccessfulSyncAt?.let { syncedAt -> HomeSyncTimestamp(syncedAt) }
    }
}

@Composable
internal fun HomeSyncTimestamp(syncedAt: Instant) {
    Text(
        text = "Last successful sync · ${DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date.from(syncedAt))}",
        color = TowerTextMuted,
        fontSize = 14.sp,
        lineHeight = 20.sp,
        modifier = Modifier.padding(top = TowerSpacing.sm)
    )
}

@Composable
private fun ErrorPanel(title: String, body: String, onRetry: () -> Unit, retryEnabled: Boolean) {
    TowerPanel(elevated = true) {
        StatusBadge(label = "REAL DATA REQUIRED", color = RiskCritical)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = title,
            color = TowerTextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = body,
            color = TowerTextMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Button(onClick = onRetry, enabled = retryEnabled) {
            Text("Retry sync")
        }
    }
}

@Composable
private fun EmptyHomePanel(title: String, body: String) {
    TowerPanel(elevated = true) {
        Text(
            text = title,
            color = TowerTextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = body,
            color = TowerTextMuted,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
    }
}
