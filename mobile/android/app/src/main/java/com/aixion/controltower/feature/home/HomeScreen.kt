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

@Composable
fun HomeScreen(
    viewModel: HomeViewModel = viewModel(),
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ForegroundRefreshEffect(onRefresh = viewModel::refreshInForeground)
    val heroTitle = when {
        state.loading -> "Loading command state"
        state.hasError -> "Backend sync failed"
        state.unsupportedApprovals.isNotEmpty() -> "${state.unsupportedApprovals.size} approvals need an app update"
        state.actionRequiredCount > 0 -> "${state.actionRequiredCount} requests require review"
        state.blockedCount > 0 -> "${state.blockedCount} blocked actions need attention"
        else -> "All clear"
    }
    val heroSubtitle = when {
        state.loading -> "Syncing approval requests."
        state.hasError -> "Authenticated screens do not show mock data. Fix the backend connection, then retry."
        state.unsupportedApprovals.isNotEmpty() -> "The app cannot safely interpret some approval status or risk values. No decisions are enabled for them."
        state.actionRequiredCount > 0 -> "AI/code execution is paused until you approve, reject, or request revision."
        state.blockedCount > 0 -> "Policy stopped unsafe work. Review the blocked queue before retrying."
        else -> "No approvals are waiting. Connect your agent and keep its computer running so work can resume after a phone decision."
    }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            HomeHeader(loading = state.loading, hasError = state.hasError)
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
                                state.unsupportedApprovals.isNotEmpty() -> "UNSUPPORTED APPROVAL"
                                state.actionRequiredCount > 0 -> "APPROVAL QUEUE"
                                else -> "COMMAND STATE"
                            },
                            color = when {
                                state.hasError -> RiskCritical
                                state.unsupportedApprovals.isNotEmpty() -> RiskCritical
                                state.actionRequiredCount > 0 -> RiskMedium
                                else -> RiskLow
                            }
                        )
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        Text(
                            text = heroTitle,
                            color = TowerTextPrimary,
                            fontSize = 28.sp,
                            fontWeight = FontWeight.SemiBold,
                            lineHeight = 33.sp
                        )
                        Spacer(modifier = Modifier.height(TowerSpacing.sm))
                        Text(
                            text = heroSubtitle,
                            color = TowerTextMuted,
                            fontSize = 14.sp,
                            lineHeight = 20.sp
                        )
                    }
                    ForgedLogoMark(size = 56.dp, color = TowerTextPrimary.copy(alpha = 0.82f))
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
        } else if (state.actionRequiredApprovals.isNotEmpty()) {
            item {
                TowerSectionHeader(
                    title = "Needs your decision",
                    subtitle = "Review the exact request before approving or rejecting it."
                )
            }
            items(state.actionRequiredApprovals.take(2)) { approval ->
                ApprovalCard(approval = approval, onClick = { onApprovalSelected(approval) })
            }
        } else if (state.unsupportedApprovals.isEmpty()) {
            item {
                EmptyHomePanel(
                    title = "No approvals waiting",
                    body = "The approval queue is clear. New requests will appear here when an agent needs your decision."
                )
            }
        }

        if (!state.hasError && state.unsupportedApprovals.isNotEmpty()) {
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
private fun HomeHeader(loading: Boolean, hasError: Boolean) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Column {
            Text(
                text = "AIXION",
                color = TowerTextPrimary,
                fontSize = 12.sp,
                fontWeight = FontWeight.SemiBold,
                letterSpacing = 2.2.sp
            )
            Text(
                text = "CONTROL TOWER",
                color = TowerTextMuted,
                fontSize = 10.sp,
                fontWeight = FontWeight.Medium,
                letterSpacing = 1.8.sp
            )
        }
        StatusBadge(
            label = homeBackendStatusLabel(loading = loading, hasError = hasError),
            color = when {
                loading -> RiskMedium
                hasError -> RiskCritical
                else -> RiskLow
            }
        )
    }
}

internal fun homeBackendStatusLabel(loading: Boolean, hasError: Boolean): String = when {
    loading -> "SYNCING"
    hasError -> "SYNC ERROR"
    else -> "SYNCED"
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
            fontSize = 13.sp,
            lineHeight = 19.sp
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
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}
