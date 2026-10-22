package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalDeliveryStatus
import com.aixion.controltower.core.model.ApprovalHistoryRequestSnapshot
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.ForegroundRefreshEffect
import com.aixion.controltower.core.ui.components.ApprovalCard
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

internal enum class ApprovalInboxSection {
    NEEDS_REVIEW,
    HISTORY
}

internal enum class ApprovalHistoryFilter {
    ALL,
    APPROVED,
    REJECTED,
    REVISION_REQUESTED
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
    section: ApprovalInboxSection
): List<ApprovalSummary> {
    val supported = approvals.filterNot(::isUnsupportedInboxApproval)
    val selected = when (section) {
        ApprovalInboxSection.NEEDS_REVIEW -> supported.filter {
            it.status == ApprovalStatus.PENDING_REVIEW ||
                it.status == ApprovalStatus.REQUESTED
        }.filterNot { approval -> approval.risk == RiskLevel.BLOCKED }

        // Mutable approval records are never a source of decision history.
        ApprovalInboxSection.HISTORY -> emptyList()
    }
    return orderApprovalsForInbox(selected)
}

internal fun decisionHistoryLabel(decision: String): String = when (decision.trim().lowercase()) {
    "approve", "approved" -> "Approved"
    "reject", "rejected", "deny", "denied" -> "Denied"
    "revise", "revision_requested" -> "Revision requested"
    else -> "Decision recorded"
}

internal fun filterApprovalHistory(
    events: List<ApprovalHistoryEvent>,
    filter: ApprovalHistoryFilter = ApprovalHistoryFilter.ALL
): List<ApprovalHistoryEvent> = events.filter { event ->
    when (filter) {
        ApprovalHistoryFilter.ALL -> true
        ApprovalHistoryFilter.APPROVED -> decisionHistoryLabel(event.decision) == "Approved"
        ApprovalHistoryFilter.REJECTED -> decisionHistoryLabel(event.decision) == "Denied"
        ApprovalHistoryFilter.REVISION_REQUESTED -> decisionHistoryLabel(event.decision) == "Revision requested"
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
        onHistorySelected = { viewModel.loadApprovalHistory() },
        onHistoryRetry = { viewModel.loadApprovalHistory(forceRefresh = true) },
        onLoadOlderHistory = viewModel::loadOlderApprovalHistory,
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
    onHistorySelected: () -> Unit = {},
    onHistoryRetry: () -> Unit = {},
    onLoadOlderHistory: () -> Unit = {},
    onApprovalSelected: (ApprovalSummary) -> Unit = {}
) {
    val palette = LocalTowerPalette.current
    val pendingCount = filterApprovalsForInbox(
        state.approvals,
        ApprovalInboxSection.NEEDS_REVIEW
    ).size
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val unsupported = state.approvals.filter(::isUnsupportedInboxApproval)
    var selectedSection by remember { mutableStateOf(ApprovalInboxSection.NEEDS_REVIEW) }
    var selectedHistoryFilter by remember { mutableStateOf(ApprovalHistoryFilter.ALL) }
    val visibleApprovals = filterApprovalsForInbox(
        approvals = state.approvals,
        section = selectedSection
    )
    val visibleHistory = filterApprovalHistory(state.approvalHistory, selectedHistoryFilter)

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.End,
                verticalAlignment = androidx.compose.ui.Alignment.CenterVertically
            ) {
                TextButton(onClick = onRetry, enabled = !state.loading && !state.refreshing) {
                    Text(if (state.refreshing) "Refreshing…" else "Refresh")
                }
            }
            Text(
                text = when {
                    state.loading && state.approvals.isEmpty() -> "Loading requests…"
                    state.refreshing -> "Refreshing. Requests are read-only until sync completes."
                    state.hasError -> "Approval sync unavailable."
                    else -> "Review pending requests or browse recorded decisions."
                },
                color = palette.inkMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
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
                    if (listOf("demo approvals", "mock approvals", "fallback approvals").none {
                            message.contains(it, ignoreCase = true)
                        }
                    ) {
                        Text(
                            text = "Demo approvals are never shown.",
                            color = palette.inkMuted,
                            fontSize = 14.sp,
                            lineHeight = 21.sp
                        )
                    }
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    Button(onClick = onRetry, enabled = !state.loading && !state.refreshing) {
                        Text("Retry approvals")
                    }
                }
            }
        }

        if (!state.hasError && selectedSection == ApprovalInboxSection.NEEDS_REVIEW) item {
            FlowRow(
                horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
            ) {
                StatusBadge("Needs review $pendingCount", palette.caution, fontSize = 14.sp, uppercase = false)
                if (blocked.isNotEmpty()) {
                    StatusBadge("Status blocked ${blocked.size}", palette.danger, fontSize = 14.sp, uppercase = false)
                }
            }
        }

        item {
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
            ) {
                ApprovalInboxSection.entries.forEach { section ->
                    FilterChip(
                        selected = selectedSection == section,
                        onClick = {
                            selectedSection = section
                            if (section == ApprovalInboxSection.HISTORY) onHistorySelected()
                        },
                        label = {
                            Text(
                                when (section) {
                                    ApprovalInboxSection.NEEDS_REVIEW -> "Needs review"
                                    ApprovalInboxSection.HISTORY -> "History"
                                }
                            )
                        },
                        colors = FilterChipDefaults.filterChipColors(
                            selectedContainerColor = palette.brand,
                            selectedLabelColor = palette.onBrand
                        )
                    )
                }
            }
        }

        if (selectedSection == ApprovalInboxSection.HISTORY) {
            item {
                Text(
                    "Immutable reviewer decisions for recent requests. Decision records do not confirm delivery to the agent or execution.",
                    color = palette.inkMuted,
                    fontSize = 14.sp,
                    lineHeight = 21.sp
                )
            }
            item {
                FlowRow(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
                    verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
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
                                        ApprovalHistoryFilter.REVISION_REQUESTED -> "Revision requested"
                                    }
                                )
                            },
                            colors = FilterChipDefaults.filterChipColors(
                                selectedContainerColor = palette.brand,
                                selectedLabelColor = palette.onBrand
                            )
                        )
                    }
                }
            }
            if (state.historyLoading) {
                item { Text("Loading decision history…", color = palette.inkMuted, fontSize = 14.sp) }
            }
            state.historyErrorMessage?.let { message ->
                item {
                    TowerPanel(elevated = true) {
                        Text("Decision history unavailable", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                        Text(message, color = palette.inkMuted, fontSize = 14.sp, lineHeight = 21.sp)
                        TextButton(onClick = onHistoryRetry, enabled = !state.historyLoading) {
                            Text(if (state.historyLoading) "Retrying…" else "Retry history")
                        }
                    }
                }
            }
        }

        val blockedRisk = state.approvals.filter { it.risk == RiskLevel.BLOCKED }
        val blockedStatus = state.approvals.filter {
            it.status == ApprovalStatus.BLOCKED && it.risk != RiskLevel.BLOCKED
        }
        val disabledApprovals = (unsupported + blockedRisk + blockedStatus).distinctBy { it.id }
        if (!state.hasError && selectedSection == ApprovalInboxSection.NEEDS_REVIEW && !state.loading && !state.refreshing && visibleApprovals.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No requests need review",
                        color = palette.ink,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "New requests will appear here when an agent needs your decision.",
                        color = palette.inkMuted,
                        fontSize = 14.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        }

        if (selectedSection == ApprovalInboxSection.HISTORY &&
            !state.historyLoading && state.historyErrorMessage == null &&
            state.historyLoaded && visibleHistory.isEmpty()
        ) {
            item {
                TowerPanel(elevated = true) {
                    Text("No decisions recorded", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
                    Text(
                        "Recorded approve, deny, and revision decisions will appear here.",
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

        if (!state.hasError && selectedSection == ApprovalInboxSection.NEEDS_REVIEW &&
            (unsupported.isNotEmpty() || blockedRisk.isNotEmpty() || blockedStatus.isNotEmpty())
        ) {
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
                            if (unsupported.isNotEmpty()) add("${unsupported.size} approval(s) contain an unrecognized status or risk value.")
                            if (blockedRisk.isNotEmpty()) add("${blockedRisk.size} approval(s) are blocked by risk policy and cannot be decided from mobile.")
                            if (blockedStatus.isNotEmpty()) add("${blockedStatus.size} approval(s) have a blocked server status and are read-only.")
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

        if (selectedSection == ApprovalInboxSection.HISTORY && state.historyErrorMessage == null) {
            items(visibleHistory, key = { it.eventId }) { event ->
                ApprovalDecisionHistoryCard(event)
            }
        }

        if (selectedSection == ApprovalInboxSection.HISTORY && state.historyLoaded && state.historyHasMore) {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
                    state.historyLoadMoreError?.let { message ->
                        Text(
                            text = message,
                            color = palette.danger,
                            fontSize = 14.sp,
                            lineHeight = 21.sp,
                            modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }
                        )
                    }
                    Button(
                        onClick = onLoadOlderHistory,
                        enabled = !state.historyLoadingMore && !state.historyLoading,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            when {
                                state.historyLoadingMore -> "Loading older decisions…"
                                state.historyLoadMoreError != null -> "Retry loading older decisions"
                                else -> "Load older decisions"
                            }
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun ApprovalDecisionHistoryCard(event: ApprovalHistoryEvent) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        StatusBadge(
            event.decision.let(::decisionHistoryLabel),
            color = when (event.decision.trim().lowercase()) {
                "approve", "approved" -> palette.positive
                "reject", "rejected", "deny", "denied" -> palette.danger
                else -> palette.caution
            },
            fontSize = 14.sp,
            uppercase = false
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text("Delivery status", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        val (deliveryLabel, deliveryExplanation, deliveryColor) = when (event.deliveryStatus) {
            ApprovalDeliveryStatus.NOT_CONFIRMED -> Triple(
                "Delivery not confirmed",
                "There is no confirmation that the connector received this decision or that the agent executed or resumed.",
                palette.caution
            )
            ApprovalDeliveryStatus.CLAIM_RECORDED -> Triple(
                "Connector claim recorded",
                "The server recorded an at-most-once claim for this decision. Connector receipt and agent execution or resumption remain unconfirmed.",
                palette.info
            )
            ApprovalDeliveryStatus.FAILURE_REPORTED -> Triple(
                "Claim response ambiguous",
                "The connector reported an ambiguous claim response. The server may have recorded the claim; connector receipt and agent execution or resumption remain unconfirmed.",
                palette.danger
            )
        }
        StatusBadge(deliveryLabel, color = deliveryColor, fontSize = 14.sp, uppercase = false)
        Text(deliveryExplanation, color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text("Decided ${formatHistoryTime(event.decidedAt)}", color = palette.inkMuted, fontSize = 14.sp)
        Text("Reviewer · ${event.reviewerLabel}", color = palette.inkMuted, fontSize = 14.sp)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Reason", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        Text(event.reason ?: "No reason was provided.", color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        val snapshot = event.requestSnapshot.takeIf { event.requestSnapshotVerified }
        if (snapshot == null) {
            Text("Request details unavailable", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
            Text(
                "This older or unverifiable record is shown without request details.",
                color = palette.inkMuted,
                style = MaterialTheme.typography.bodyLarge
            )
        } else {
            VerifiedRequestSnapshot(snapshot)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
    }
}

@Composable
private fun VerifiedRequestSnapshot(snapshot: ApprovalHistoryRequestSnapshot) {
    val palette = LocalTowerPalette.current
    Text("Verified request", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
    Text(snapshot.title?.takeIf(String::isNotBlank) ?: "Untitled request", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
    Text(snapshot.summary?.takeIf(String::isNotBlank) ?: "Request summary unavailable", color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
    val provider = listOfNotNull(
        snapshot.sourceAgentName?.takeIf(String::isNotBlank) ?: snapshot.agentName?.takeIf(String::isNotBlank),
        snapshot.sourceProvider?.takeIf(String::isNotBlank)
    ).distinct().joinToString(" · ")
    if (provider.isNotBlank()) Text(provider, color = palette.inkMuted, fontSize = 14.sp)
    val risk = snapshot.riskLevel.name.lowercase().replaceFirstChar { it.uppercase() }
    StatusBadge(
        if (snapshot.riskLevel == RiskLevel.UNKNOWN) "Risk unavailable" else "$risk risk",
        color = if (snapshot.riskLevel == RiskLevel.UNKNOWN) palette.caution else palette.brand,
        fontSize = 14.sp,
        uppercase = false
    )
    snapshot.targetBranch?.takeIf(String::isNotBlank)?.let { branch ->
        Text("Target branch · $branch", color = palette.inkMuted, fontSize = 14.sp)
    }
    if (snapshot.files.isNotEmpty()) {
        Text("Affected files", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        snapshot.files.forEach { file -> Text(file.path, color = palette.inkMuted, fontSize = 14.sp, lineHeight = 21.sp) }
    }
    if (snapshot.testPlan.isNotEmpty()) {
        Text("Test plan", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        snapshot.testPlan.forEach { Text(it, color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge) }
    }
    snapshot.rollbackPlan?.takeIf(String::isNotBlank)?.let { plan ->
        Text("Rollback plan", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        Text(plan, color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
    }
}

private fun formatHistoryTime(raw: String?): String {
    if (raw.isNullOrBlank()) return "time unavailable"
    return runCatching {
        val formatter = DateTimeFormatter.ofPattern("MMM d, yyyy 'at' h:mm a", Locale.getDefault())
            .withZone(ZoneId.systemDefault())
        formatter.format(Instant.parse(raw))
    }.getOrDefault("time unavailable")
}
