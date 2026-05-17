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
import com.aixion.controltower.core.model.ApprovalDashboardBucket
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.dashboardBucket
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskBlocked
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

enum class ApprovalInboxFilter(val label: String, val emptyTitle: String, val emptyBody: String) {
    ALL(
        label = "All",
        emptyTitle = "No approvals found",
        emptyBody = "There are no approval records in the current backend response."
    ),
    ACTION(
        label = "Action",
        emptyTitle = "No approvals waiting",
        emptyBody = "No pending approval decisions are waiting right now."
    ),
    BLOCKED(
        label = "Blocked",
        emptyTitle = "No blocked approvals",
        emptyBody = "No policy-blocked approval requests are currently waiting."
    ),
    EXECUTION(
        label = "Execution",
        emptyTitle = "No approved execution queue",
        emptyBody = "No approved work is currently waiting for execution or PR progress."
    )
}

@Composable
fun ApprovalInboxScreen(
    viewModel: ApprovalsViewModel = viewModel(),
    filter: ApprovalInboxFilter = ApprovalInboxFilter.ALL,
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ApprovalInboxContent(
        state = state,
        filter = filter,
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
    filter: ApprovalInboxFilter = ApprovalInboxFilter.ALL,
    onRetry: () -> Unit = {},
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val pending = state.approvals.filter { it.status == ApprovalStatus.PENDING_REVIEW || it.status == ApprovalStatus.REQUESTED }
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val approved = state.approvals.filter { it.status == ApprovalStatus.APPROVED }
    val visibleApprovals = state.approvals.filterBy(filter)

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Approvals", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = when {
                    state.loading -> "Loading approval decisions..."
                    state.hasError -> "Backend approval sync failed. No mock approvals are shown."
                    filter == ApprovalInboxFilter.ALL -> "Allow, deny, or request revision before connected-agent work continues."
                    else -> "Filtered by ${filter.label}. Home card counts should land on matching approval work."
                },
                color = TowerTextMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
        }

        item {
            TowerPanel(elevated = true) {
                Text("Decision gate", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "External agents can submit work, but they do not approve it. This queue is where the operator decides whether work may continue, must stop, or needs revision.",
                    color = TowerTextMuted,
                    fontSize = 13.sp,
                    lineHeight = 19.sp
                )
                if (filter != ApprovalInboxFilter.ALL) {
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    StatusBadge("FILTER ${filter.label.uppercase()}", TowerAccent)
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
                        fontSize = 13.sp,
                        lineHeight = 19.sp
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
                StatusBadge("Pending ${pending.size}", if (filter == ApprovalInboxFilter.ACTION) TowerAccent else RiskMedium)
                StatusBadge("Blocked ${blocked.size}", if (filter == ApprovalInboxFilter.BLOCKED) TowerAccent else RiskBlocked)
                StatusBadge("Approved ${approved.size}", if (filter == ApprovalInboxFilter.EXECUTION) TowerAccent else RiskLow)
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
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        } else if (!state.loading && visibleApprovals.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = filter.emptyTitle,
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = filter.emptyBody,
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
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

private fun List<ApprovalSummary>.filterBy(filter: ApprovalInboxFilter): List<ApprovalSummary> {
    return when (filter) {
        ApprovalInboxFilter.ALL -> this
        ApprovalInboxFilter.ACTION -> filter { approval -> approval.dashboardBucket == ApprovalDashboardBucket.NEEDS_APPROVAL }
        ApprovalInboxFilter.BLOCKED -> filter { approval -> approval.status == ApprovalStatus.BLOCKED }
        ApprovalInboxFilter.EXECUTION -> filter { approval ->
            approval.dashboardBucket == ApprovalDashboardBucket.APPROVED_WAITING ||
                approval.dashboardBucket == ApprovalDashboardBucket.EXECUTING ||
                approval.dashboardBucket == ApprovalDashboardBucket.READY_FOR_PR_REVIEW
        }
    }
}