package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.TrustExceptionDto
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
        emptyBody = "No pending native-agent or work approval decisions are waiting right now."
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
        },
        onNativeApprove = { actionId ->
            viewModel.decideNativeAction(actionId, allow = true)
        },
        onNativeBlock = { actionId ->
            viewModel.decideNativeAction(actionId, allow = false)
        }
    )
}

@Composable
fun ApprovalInboxContent(
    state: ApprovalsUiState,
    filter: ApprovalInboxFilter = ApprovalInboxFilter.ALL,
    onRetry: () -> Unit = {},
    onApprovalSelected: (ApprovalSummary) -> Unit = {},
    onNativeApprove: (String) -> Unit = {},
    onNativeBlock: (String) -> Unit = {}
) {
    val pending = state.approvals.filter {
        it.status == ApprovalStatus.PENDING_REVIEW || it.status == ApprovalStatus.REQUESTED
    }
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val approved = state.approvals.filter { it.status == ApprovalStatus.APPROVED }
    val visibleApprovals = state.approvals.filterBy(filter)
    val visibleNativeActions = state.nativeActions.filterBy(filter)

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
                    filter == ApprovalInboxFilter.ALL -> "Approve or deny exact actions from native agents without leaving their original conversation."
                    else -> "Filtered by ${filter.label}. Home card counts include native-agent actions."
                },
                color = TowerTextMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
        }

        item {
            TowerPanel(elevated = true) {
                Text("Exact decision gate", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "For native agents, the decision is bound to the immutable command or target paths, workspace, provider conversation, and payload hash. Aixion does not launch a replacement agent.",
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
                StatusBadge(
                    "Pending ${pending.size + state.pendingNativeActions.size}",
                    if (filter == ApprovalInboxFilter.ACTION) TowerAccent else RiskMedium
                )
                StatusBadge(
                    "Blocked ${blocked.size + state.blockedNativeActions.size}",
                    if (filter == ApprovalInboxFilter.BLOCKED) TowerAccent else RiskBlocked
                )
                StatusBadge(
                    "Approved ${approved.size}",
                    if (filter == ApprovalInboxFilter.EXECUTION) TowerAccent else RiskLow
                )
            }
        }

        if (state.lastActionMessage != null) {
            item {
                TowerPanel(elevated = false) {
                    StatusBadge("DECISION RECORDED", RiskLow)
                    Text(
                        state.lastActionMessage,
                        color = TowerTextPrimary,
                        fontSize = 13.sp
                    )
                }
            }
        }

        if (state.hasError && visibleApprovals.isEmpty() && visibleNativeActions.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No fallback approvals loaded",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Authenticated approval screens require real backend data instead of silently rendering demo approvals. Use Retry after the backend is reachable.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        } else if (!state.loading && visibleApprovals.isEmpty() && visibleNativeActions.isEmpty()) {
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
            items(visibleNativeActions, key = { it.id }) { action ->
                NativeExactActionCard(
                    action = action,
                    decidingActionId = state.decidingNativeActionId,
                    onApprove = onNativeApprove,
                    onBlock = onNativeBlock
                )
            }
            items(visibleApprovals, key = { it.id }) { approval ->
                ApprovalCard(
                    approval = approval,
                    onClick = { onApprovalSelected(approval) }
                )
            }
        }
    }
}

@Composable
private fun NativeExactActionCard(
    action: TrustExceptionDto,
    decidingActionId: String?,
    onApprove: (String) -> Unit,
    onBlock: (String) -> Unit
) {
    val actionId = action.actionId
    val pending = action.isPendingExactAction && actionId != null
    val exactTarget = when {
        !action.command.isNullOrBlank() -> action.command
        action.paths.isNotEmpty() -> action.paths.joinToString(separator = "\n")
        action.networkDomains.isNotEmpty() -> action.networkDomains.joinToString(separator = "\n")
        else -> "No executable target was supplied. Deny unless this is expected."
    }

    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(action.provider ?: "NATIVE AGENT", TowerAccent)
            StatusBadge(action.actionType ?: action.category, if (pending) RiskMedium else RiskBlocked)
        }
        Text(
            text = action.title,
            color = TowerTextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            text = exactTarget,
            color = TowerTextPrimary,
            fontSize = 12.sp,
            lineHeight = 17.sp,
            fontFamily = FontFamily.Monospace
        )
        action.cwd?.let {
            Text("Working directory\n$it", color = TowerTextMuted, fontSize = 11.sp)
        }
        action.repository?.let {
            Text(
                "Repository $it${action.branch?.let { branch -> " • $branch" } ?: ""}",
                color = TowerTextMuted,
                fontSize = 11.sp
            )
        }
        action.nativeConversationId?.let {
            Text(
                "Native conversation $it\nStep ${action.nativeStepIndex ?: "unknown"}",
                color = TowerTextMuted,
                fontSize = 11.sp
            )
        }
        action.relaySessionId?.let {
            Text("Aixion session $it", color = TowerTextMuted, fontSize = 10.sp)
        }
        action.adapterId?.let {
            Text("Adapter $it", color = TowerTextMuted, fontSize = 10.sp)
        }
        action.providerPayloadSha256?.let {
            Text(
                "Provider payload SHA-256\n$it",
                color = TowerTextMuted,
                fontSize = 10.sp,
                fontFamily = FontFamily.Monospace
            )
        }
        action.actionPayloadSha256?.let {
            Text(
                "Aixion action SHA-256\n$it",
                color = TowerTextMuted,
                fontSize = 10.sp,
                fontFamily = FontFamily.Monospace
            )
        }
        Text(action.summary, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)

        if (pending && actionId != null) {
            Button(
                onClick = { onApprove(actionId) },
                enabled = decidingActionId == null,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(if (decidingActionId == actionId) "Recording…" else "Approve exact action")
            }
            OutlinedButton(
                onClick = { onBlock(actionId) },
                enabled = decidingActionId == null,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Deny exact action")
            }
        }
    }
}

private fun List<ApprovalSummary>.filterBy(filter: ApprovalInboxFilter): List<ApprovalSummary> {
    return when (filter) {
        ApprovalInboxFilter.ALL -> this
        ApprovalInboxFilter.ACTION -> filter { approval ->
            approval.dashboardBucket == ApprovalDashboardBucket.NEEDS_APPROVAL
        }
        ApprovalInboxFilter.BLOCKED -> filter { approval ->
            approval.status == ApprovalStatus.BLOCKED
        }
        ApprovalInboxFilter.EXECUTION -> filter { approval ->
            approval.dashboardBucket == ApprovalDashboardBucket.APPROVED_WAITING ||
                approval.dashboardBucket == ApprovalDashboardBucket.EXECUTING ||
                approval.dashboardBucket == ApprovalDashboardBucket.READY_FOR_PR_REVIEW
        }
    }
}

private fun List<TrustExceptionDto>.filterBy(
    filter: ApprovalInboxFilter
): List<TrustExceptionDto> {
    return when (filter) {
        ApprovalInboxFilter.ALL -> filter { it.actionId != null }
        ApprovalInboxFilter.ACTION -> filter { it.isPendingExactAction }
        ApprovalInboxFilter.BLOCKED -> filter {
            it.category == "BLOCK" && it.actionId != null
        }
        ApprovalInboxFilter.EXECUTION -> emptyList()
    }
}
