package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

internal enum class ApprovalInboxSection {
    NEEDS_REVIEW,
    HISTORY
}

internal enum class ApprovalHistoryFilter {
    ALL,
    APPROVED,
    REJECTED,
    EXPIRED
}

internal fun orderApprovalsForInbox(approvals: List<ApprovalSummary>): List<ApprovalSummary> =
    approvals.sortedWith(
        compareBy<ApprovalSummary> {
            when {
                it.status == ApprovalStatus.PENDING_REVIEW || it.status == ApprovalStatus.REQUESTED -> 0
                it.status == ApprovalStatus.BLOCKED || it.status == ApprovalStatus.REVISION_REQUESTED -> 1
                it.status == ApprovalStatus.UNKNOWN || it.risk == RiskLevel.UNKNOWN -> 2
                else -> 3
            }
        }.thenBy { approval ->
            if (approval.status == ApprovalStatus.PENDING_REVIEW || approval.status == ApprovalStatus.REQUESTED) {
                approval.expiresAt?.let { raw ->
                    runCatching { java.time.Instant.parse(raw) }.getOrNull()
                } ?: java.time.Instant.MAX
            } else {
                java.time.Instant.MAX
            }
        }
    )

internal fun isUnsupportedInboxApproval(approval: ApprovalSummary): Boolean =
    approval.status == ApprovalStatus.UNKNOWN || approval.risk == RiskLevel.UNKNOWN

internal fun filterApprovalsForInbox(
    approvals: List<ApprovalSummary>,
    section: ApprovalInboxSection,
    historyFilter: ApprovalHistoryFilter = ApprovalHistoryFilter.ALL
): List<ApprovalSummary> {
    val supported = approvals.filterNot(::isUnsupportedInboxApproval)
    val selected = when (section) {
        ApprovalInboxSection.NEEDS_REVIEW -> supported.filter {
            it.status == ApprovalStatus.PENDING_REVIEW ||
                it.status == ApprovalStatus.REQUESTED
        }.filterNot { approval ->
            approval.risk == RiskLevel.BLOCKED
        }

        ApprovalInboxSection.HISTORY -> supported.filter { approval ->
            when (historyFilter) {
                ApprovalHistoryFilter.ALL -> approval.status !in setOf(
                    ApprovalStatus.PENDING_REVIEW,
                    ApprovalStatus.REQUESTED
                )

                ApprovalHistoryFilter.APPROVED -> approval.status == ApprovalStatus.APPROVED

                ApprovalHistoryFilter.REJECTED ->
                    approval.status == ApprovalStatus.DENIED ||
                        approval.status == ApprovalStatus.REJECTED

                ApprovalHistoryFilter.EXPIRED -> approval.status == ApprovalStatus.EXPIRED
            }
        }
    }
    return orderApprovalsForInbox(selected)
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
@OptIn(ExperimentalLayoutApi::class)
fun ApprovalInboxContent(
    state: ApprovalsUiState,
    onRetry: () -> Unit = {},
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val palette = LocalTowerPalette.current
    val pendingCount = filterApprovalsForInbox(
        state.approvals,
        ApprovalInboxSection.NEEDS_REVIEW
    ).size
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val approved = state.approvals.filter { it.status == ApprovalStatus.APPROVED }
    val unsupported = state.approvals.filter(::isUnsupportedInboxApproval)
    var selectedSection by remember { mutableStateOf(ApprovalInboxSection.NEEDS_REVIEW) }
    var selectedHistoryFilter by remember { mutableStateOf(ApprovalHistoryFilter.ALL) }
    val visibleApprovals = filterApprovalsForInbox(
        approvals = state.approvals,
        section = selectedSection,
        historyFilter = selectedHistoryFilter
    )

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Approvals", color = palette.ink, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = when {
                    state.loading && state.approvals.isEmpty() -> "Loading approval decisions..."
                    state.refreshing -> "Refreshing approvals. Showing the last synced data; opening requests is paused until sync completes."
                    state.hasError -> "Backend approval sync failed. No mock approvals are shown."
                    else -> "Review requests from connected agents and check their current status here."
                },
                color = palette.inkMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Button(onClick = onRetry, enabled = !state.loading && !state.refreshing) {
                Text(if (state.loading) "Refreshing approvals..." else "Refresh approvals")
            }
        }

        item {
            TowerPanel(elevated = true) {
                Text("Decision gate", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "External agents can submit work, but they do not approve it. This queue is where the operator decides whether work may continue, must stop, or needs revision.",
                    color = palette.inkMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
            }
        }

        state.errorMessage?.let { message ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge("Real data required", palette.danger, fontSize = 14.sp, uppercase = false)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "Approval data unavailable",
                        color = palette.ink,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = message,
                        color = palette.inkMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    Button(onClick = onRetry, enabled = !state.loading && !state.refreshing) {
                        Text("Retry approvals")
                    }
                }
            }
        }

        if (!state.hasError) item {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                StatusBadge("Needs review $pendingCount", palette.caution, fontSize = 14.sp, uppercase = false)
                StatusBadge("Status blocked ${blocked.size}", palette.danger, fontSize = 14.sp, uppercase = false)
                StatusBadge("Approved ${approved.size}", palette.positive, fontSize = 14.sp, uppercase = false)
            }
        }

        item {
            Row(horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
                ApprovalInboxSection.entries.forEach { section ->
                    FilterChip(
                        selected = selectedSection == section,
                        onClick = { selectedSection = section },
                        label = {
                            Text(
                                when (section) {
                                    ApprovalInboxSection.NEEDS_REVIEW -> "Needs review"
                                    ApprovalInboxSection.HISTORY -> "History"
                                }
                            )
                        }
                    )
                }
            }
        }

        if (selectedSection == ApprovalInboxSection.HISTORY) {
            item {
                Text(
                    "History uses the current approval status. The original decision, reviewer, time, and delivery outcome are not available in this list yet.",
                    color = palette.inkMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
            }
            item {
                Row(
                    modifier = Modifier.horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
                ) {
                    ApprovalHistoryFilter.entries.forEach { filter ->
                        FilterChip(
                            selected = selectedHistoryFilter == filter,
                            onClick = { selectedHistoryFilter = filter },
                            label = {
                                Text(
                                    when (filter) {
                                        ApprovalHistoryFilter.ALL -> "All"
                                        ApprovalHistoryFilter.APPROVED -> "Approved"
                                        ApprovalHistoryFilter.REJECTED -> "Rejected"
                                        ApprovalHistoryFilter.EXPIRED -> "Expired"
                                    }
                                )
                            }
                        )
                    }
                }
            }
        }

        if (state.hasError) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No fallback approvals loaded",
                        color = palette.ink,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Authenticated approval screens now require real backend data instead of silently rendering demo approvals. Use Retry after the backend is reachable.",
                        color = palette.inkMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        }

        val blockedRisk = state.approvals.filter { it.risk == RiskLevel.BLOCKED }
        val blockedStatus = state.approvals.filter {
            it.status == ApprovalStatus.BLOCKED && it.risk != RiskLevel.BLOCKED
        }
        val disabledApprovals = (unsupported + blockedRisk + blockedStatus).distinctBy { it.id }
        if (!state.hasError && (unsupported.isNotEmpty() || blockedRisk.isNotEmpty() || blockedStatus.isNotEmpty())) {
            item {
                TowerPanel(elevated = true) {
                    StatusBadge(
                        if (unsupported.isNotEmpty()) "Unsupported data · decisions disabled"
                        else "Policy blocked · decisions disabled",
                        palette.danger,
                        fontSize = 14.sp,
                        uppercase = false
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = buildList {
                            if (unsupported.isNotEmpty()) {
                                add("${unsupported.size} approval(s) contain an unrecognized status or risk value.")
                            }
                            if (blockedRisk.isNotEmpty()) {
                                add("${blockedRisk.size} approval(s) are blocked by risk policy and cannot be decided from mobile.")
                            }
                            if (blockedStatus.isNotEmpty()) {
                                add("${blockedStatus.size} approval(s) have a blocked server status and are read-only.")
                            }
                            add("These requests remain visible below for review; update the app or backend if the data is unsupported.")
                        }.joinToString(" "),
                        color = palette.inkMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
            items(disabledApprovals) { approval ->
                ApprovalCard(
                    approval = approval,
                    onClick = if (canSelectApprovalFromInbox(state)) {
                        { onApprovalSelected(approval) }
                    } else {
                        null
                    }
                )
            }
        }

        if (!state.hasError && !state.loading && !state.refreshing && visibleApprovals.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = when (selectedSection) {
                            ApprovalInboxSection.NEEDS_REVIEW -> "No requests need review"
                            ApprovalInboxSection.HISTORY -> "No requests match this filter"
                        },
                        color = palette.ink,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = when (selectedSection) {
                            ApprovalInboxSection.NEEDS_REVIEW -> "New requests will appear here when an agent needs your decision."
                            ApprovalInboxSection.HISTORY -> "Try another status filter to see more requests."
                        },
                        color = palette.inkMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        }

        if (!state.hasError) {
            items(visibleApprovals) { approval ->
                ApprovalCard(
                    approval = approval,
                    onClick = if (canSelectApprovalFromInbox(state)) {
                        { onApprovalSelected(approval) }
                    } else {
                        null
                    }
                )
            }
        }
    }
}
