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
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.StatusCard
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
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
    val heroTitle = when {
        state.loading -> "Loading command state"
        state.hasError -> "Backend sync failed"
        state.actionRequiredCount > 0 -> "${state.actionRequiredCount} requests require review"
        state.blockedCount > 0 -> "${state.blockedCount} blocked actions need attention"
        else -> "All clear"
    }
    val heroSubtitle = when {
        state.loading -> "Syncing approvals, worker state, and execution queues."
        state.hasError -> "Authenticated screens do not show mock data. Fix the backend connection, then retry."
        state.actionRequiredCount > 0 -> "AI/code execution stays paused until the right human decision exists."
        state.blockedCount > 0 -> "Policy stopped unsafe work. Review the blocked queue before retrying."
        else -> "No human action needed right now. Keep building, but keep the tower watching."
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
                                state.actionRequiredCount > 0 -> "APPROVAL QUEUE"
                                else -> "COMMAND STATE"
                            },
                            color = when {
                                state.hasError -> RiskCritical
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

        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                StatusCard(
                    title = "Human Action",
                    value = state.actionRequiredCount.toString(),
                    subtitle = "review, unblock, PR",
                    accent = TowerAccent,
                    modifier = Modifier.weight(1f)
                )
                StatusCard(
                    title = "Runner Queue",
                    value = state.githubExecutionCount.toString(),
                    subtitle = "approved, not PR-ready",
                    accent = RiskMedium,
                    modifier = Modifier.weight(1f)
                )
            }
        }

        item {
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                StatusCard(
                    title = "Blocked",
                    value = state.blockedCount.toString(),
                    subtitle = "policy stopped",
                    accent = RiskBlocked,
                    modifier = Modifier.weight(1f)
                )
                StatusCard(
                    title = "Failed Tests",
                    value = state.failedTestsCount.toString(),
                    subtitle = "need review",
                    accent = RiskCritical,
                    modifier = Modifier.weight(1f)
                )
            }
        }

        item {
            TowerSectionHeader(
                title = "Action Required",
                subtitle = "Requests that need human judgment before execution can continue."
            )
        }

        if (state.hasError) {
            item {
                EmptyHomePanel(
                    title = "No fallback approvals loaded",
                    body = "This is intentional. Authenticated product screens now wait for real backend data instead of silently showing demo data. Use Retry after the backend is reachable."
                )
            }
        } else if (state.actionRequiredApprovals.isEmpty()) {
            item {
                EmptyHomePanel(
                    title = "No approvals waiting",
                    body = "The approval queue is clear. New AI/code actions will appear here when review is required."
                )
            }
        } else {
            items(state.actionRequiredApprovals.take(3)) { approval ->
                ApprovalCard(approval = approval, onClick = { onApprovalSelected(approval) })
            }
        }

        if (!state.hasError && state.githubExecutionApprovals.isNotEmpty()) {
            item {
                TowerSectionHeader(
                    title = "Awaiting GitHub Execution",
                    subtitle = "Approved work that still needs branch, validation, or PR completion."
                )
            }
            items(state.githubExecutionApprovals.take(3)) { approval ->
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
            label = when {
                loading -> "SYNCING"
                hasError -> "OFFLINE"
                else -> "CONNECTED"
            },
            color = when {
                loading -> RiskMedium
                hasError -> RiskCritical
                else -> RiskLow
            }
        )
    }
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