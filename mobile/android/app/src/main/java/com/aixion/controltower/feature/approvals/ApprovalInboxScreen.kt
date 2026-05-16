package com.aixion.controltower.feature.approvals

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

@Composable
fun ApprovalInboxScreen(
    viewModel: ApprovalsViewModel = viewModel(),
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ApprovalInboxContent(
        state = state,
        onApprovalSelected = { approval ->
            viewModel.selectApproval(approval)
            onApprovalSelected(approval)
        }
    )
}

@Composable
fun ApprovalInboxContent(
    state: ApprovalsUiState,
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val pending = state.approvals.filter { it.status == ApprovalStatus.PENDING_REVIEW || it.status == ApprovalStatus.REQUESTED }
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val approved = state.approvals.filter { it.status == ApprovalStatus.APPROVED }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Approval Inbox", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = when {
                    state.loading -> "Loading approvals..."
                    state.hasError -> "Backend approval sync failed. No mock approvals are shown."
                    else -> "Review agent requests before code moves."
                },
                color = TowerTextMuted,
                fontSize = 14.sp
            )
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
                        text = "Authenticated approval screens now require real backend data instead of silently rendering demo approvals.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        } else {
            items(state.approvals) { approval ->
                ApprovalCard(
                    approval = approval,
                    onClick = { onApprovalSelected(approval) }
                )
            }
        }
    }
}