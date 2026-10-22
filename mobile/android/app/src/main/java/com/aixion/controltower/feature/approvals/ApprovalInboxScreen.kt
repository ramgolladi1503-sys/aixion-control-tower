package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.ForegroundRefreshEffect
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

internal fun orderApprovalsForInbox(approvals: List<ApprovalSummary>): List<ApprovalSummary> =
    approvals.sortedBy { approval ->
        when {
            approval.status == ApprovalStatus.PENDING_REVIEW || approval.status == ApprovalStatus.REQUESTED -> 0
            approval.status == ApprovalStatus.BLOCKED -> 1
            approval.status == ApprovalStatus.UNKNOWN || approval.risk == RiskLevel.UNKNOWN -> 2
            else -> 3
        }
    }

@Composable
fun ApprovalInboxScreen(
    viewModel: ApprovalsViewModel = viewModel(),
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ForegroundRefreshEffect(onRefresh = viewModel::refreshInForeground)
    ApprovalInboxContent(
        state = state,
        onRetry = viewModel::refresh,
        onApprovalSelected = { approval ->
            viewModel.selectApproval(approval)
            onApprovalSelected(approval)
        }
    )
}
@Composable
fun ApprovalInboxContent(
    state: ApprovalsUiState,
    onRetry: () -> Unit = {},
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val pending = state.approvals.filter { it.status == ApprovalStatus.PENDING_REVIEW || it.status == ApprovalStatus.REQUESTED }
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val approved = state.approvals.filter { it.status == ApprovalStatus.APPROVED }
    val unsupported = state.approvals.filter {
        it.status == ApprovalStatus.UNKNOWN || it.risk == RiskLevel.UNKNOWN
    }
    val visibleApprovals = orderApprovalsForInbox(state.approvals)

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Approvals", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = when {
                    state.loading -> "Loading approval decisions..."
                    state.hasError -> "Backend approval sync failed. No mock approvals are shown."
                    else -> "Review requests from connected agents, then see every decision and its status here."
                },
                color = TowerTextMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Button(onClick = onRetry, enabled = !state.loading) {
                Text(if (state.loading) "Refreshing approvals..." else "Refresh approvals")
            }
        }

        item {
            TowerPanel(elevated = true) {
                Text("Decision gate", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "External agents can submit work, but they do not approve it. This queue is where the operator decides whether work may continue, must stop, or needs revision.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
            }
        }

        if (unsupported.isNotEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    StatusBadge("UNSUPPORTED DATA · DECISIONS DISABLED", RiskCritical)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "${unsupported.size} approval(s) contain a status or risk value this app does not recognize. Update the app or backend before deciding.",
                        color = TowerTextMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        }

        state.errorMessage?.let { message ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge("REAL DATA REQUIRED", RiskCritical)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "Approval data unavailable",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = message,
                        color = TowerTextMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    Button(onClick = onRetry, enabled = !state.loading) {
                        Text("Retry approvals")
                    }
                }
            }
        }

        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                StatusBadge("Pending ${pending.size}", RiskMedium)
                StatusBadge("Blocked ${blocked.size}", RiskBlocked)
                StatusBadge("Approved ${approved.size}", RiskLow)
            }
        }

        if (state.hasError) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No fallback approvals loaded",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Authenticated approval screens now require real backend data instead of silently rendering demo approvals. Use Retry after the backend is reachable.",
                        color = TowerTextMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        } else if (!state.loading && visibleApprovals.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No approvals yet",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Requests from connected agents and your past decisions will appear here.",
                        color = TowerTextMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        } else {
            items(visibleApprovals) { approval ->
                ApprovalCard(
                    approval = approval,
                    onClick = { onApprovalSelected(approval) }
                )
            }
        }
    }
}
