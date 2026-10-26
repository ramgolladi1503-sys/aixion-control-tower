package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
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
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding
import java.text.DateFormat
import java.time.Instant
import java.util.Date
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

/** Terminal requests belong in history for context, but never receive decision actions. */
internal fun terminalApprovalRequests(approvals: List<ApprovalSummary>): List<ApprovalSummary> =
    approvals.filter { it.status == ApprovalStatus.EXPIRED || it.status == ApprovalStatus.CANCELLED }
        .sortedByDescending { approval ->
            approval.expiresAt?.let { raw -> runCatching { Instant.parse(raw) }.getOrNull() }
                ?: Instant.MIN
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
    onApprovalSelected: (ApprovalSummary) -> Unit = {},
    onOpenConnect: () -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    ForegroundRefreshEffect(onRefresh = viewModel::refreshInForeground)
    ApprovalInboxContent(
        state = state,
        onRetry = viewModel::refresh,
        onHistorySelected = { viewModel.loadApprovalHistory(forceRefresh = true) },
        onHistoryRetry = { viewModel.loadApprovalHistory(forceRefresh = true) },
        onLoadOlderHistory = viewModel::loadOlderApprovalHistory,
        onOpenConnect = onOpenConnect,
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
    onApprovalSelected: (ApprovalSummary) -> Unit = {},
    onOpenConnect: () -> Unit = {}
) {
    val palette = LocalTowerPalette.current
    val pendingCount = filterApprovalsForInbox(
        state.approvals,
        ApprovalInboxSection.NEEDS_REVIEW
    ).size
    val blocked = state.approvals.filter { it.status == ApprovalStatus.BLOCKED }
    val unsupported = state.approvals.filter(::isUnsupportedInboxApproval)
    var selectedSection by rememberSaveable { mutableStateOf(ApprovalInboxSection.NEEDS_REVIEW) }
    var selectedHistoryFilter by rememberSaveable { mutableStateOf(ApprovalHistoryFilter.ALL) }
    var technicalDetailsExpanded by remember(state.errorMessage) { mutableStateOf(false) }
    val visibleApprovals = filterApprovalsForInbox(
        approvals = state.approvals,
        section = selectedSection
    )
    val visibleHistory = filterApprovalHistory(state.approvalHistory, selectedHistoryFilter)
    val terminalRequests = terminalApprovalRequests(state.approvals)
    val refreshSelectedContent = {
        onRetry()
        if (selectedSection == ApprovalInboxSection.HISTORY) {
            onHistoryRetry()
        }
    }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.md)
    ) {
        item {
            TowerHeroPanel(
                modifier = Modifier.testTag("approval-inbox-heading-panel"),
                compact = true
            ) {
                Text(
                    text = when (selectedSection) {
                        ApprovalInboxSection.NEEDS_REVIEW -> "Needs your decision"
                        ApprovalInboxSection.HISTORY -> "Decision history"
                    },
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("approval-inbox-heading")
                        .semantics { heading() },
                    color = palette.ink,
                    style = MaterialTheme.typography.headlineSmall
                )
                Spacer(modifier = Modifier.height(TowerSpacing.xs))
                Text(
                    text = when (selectedSection) {
                        ApprovalInboxSection.NEEDS_REVIEW -> "Review the exact request before deciding."
                        ApprovalInboxSection.HISTORY -> "Recorded decisions and their delivery status."
                    },
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodySmall
                )
            }
        }

        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = androidx.compose.ui.Alignment.CenterVertically
            ) {
                Text(
                    text = when {
                        state.loading && state.approvals.isEmpty() && !state.hasError -> "Loading approval requests…"
                        state.refreshing -> "Refreshing. Requests are read-only until sync completes."
                        state.hasError -> "Approval sync unavailable."
                        state.lastSuccessfulSyncAt != null -> "Last checked · ${DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date.from(state.lastSuccessfulSyncAt))}"
                        else -> "Status not verified"
                    },
                    modifier = Modifier
                        .weight(1f)
                        .testTag("approval-inbox-sync-status")
                        .then(
                            if (state.hasError) Modifier
                            else Modifier.semantics { liveRegion = LiveRegionMode.Polite }
                        ),
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodySmall
                )
                if (!state.hasError) {
                    TextButton(
                        onClick = refreshSelectedContent,
                        enabled = !state.loading && !state.refreshing
                    ) {
                        Text(if (state.refreshing) "Refreshing…" else "Refresh")
                    }
                }
            }
        }

        state.errorMessage?.let { message ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge("Connection issue", palette.danger, fontSize = MaterialTheme.typography.labelLarge.fontSize, uppercase = false)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "Approval data unavailable",
                        modifier = Modifier.semantics { heading() },
                        color = palette.ink,
                        style = MaterialTheme.typography.titleLarge
                    )
                    Text(
                        text = "Reconnect and retry to load the latest approvals. Requests stay unavailable until the server responds.",
                        color = palette.inkMuted,
                        style = MaterialTheme.typography.bodyMedium
                    )
                    if (state.approvals.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(TowerSpacing.sm))
                        state.lastSuccessfulSyncAt?.let { syncedAt ->
                            Text(
                                text = "Last successful check · ${DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date.from(syncedAt))}",
                                modifier = Modifier
                                    .testTag("approval-inbox-last-successful-check"),
                                color = palette.inkMuted,
                                style = MaterialTheme.typography.bodySmall
                            )
                        }
                        Text(
                            text = "The latest approval check failed. Previously loaded requests are hidden because their status may have changed. Reconnect and retry before reviewing.",
                            modifier = Modifier
                                .testTag("approval-inbox-stale-data-warning")
                                .semantics { liveRegion = LiveRegionMode.Polite },
                            color = palette.danger,
                            style = MaterialTheme.typography.bodyMedium
                        )
                    }
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    Button(onClick = refreshSelectedContent, enabled = !state.loading && !state.refreshing) {
                        Text("Retry approvals")
                    }
                    TextButton(
                        onClick = { technicalDetailsExpanded = !technicalDetailsExpanded },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (technicalDetailsExpanded) "Hide technical details" else "Technical details")
                    }
                    if (technicalDetailsExpanded) {
                        Text(
                            text = message,
                            color = palette.inkMuted,
                            style = MaterialTheme.typography.bodySmall
                        )
                    }
                }
            }
        }

        item {
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
            ) {
                if (
                    !state.hasError &&
                    selectedSection == ApprovalInboxSection.NEEDS_REVIEW &&
                    pendingCount > 0
                ) {
                    StatusBadge(
                        "$pendingCount pending",
                        palette.caution,
                        fontSize = MaterialTheme.typography.labelLarge.fontSize,
                        uppercase = false,
                        modifier = Modifier.testTag("approval-inbox-pending-count")
                    )
                }
                if (!state.hasError && selectedSection == ApprovalInboxSection.NEEDS_REVIEW && blocked.isNotEmpty()) {
                    StatusBadge(
                        "Status blocked ${blocked.size}",
                        palette.danger,
                        fontSize = MaterialTheme.typography.labelLarge.fontSize,
                        uppercase = false
                    )
                }
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
                    style = MaterialTheme.typography.bodyMedium
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
                                        ApprovalHistoryFilter.REJECTED -> "Denied"
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
                item {
                    Text(
                        text = "Loading decision history…",
                        modifier = Modifier
                            .testTag("approval-history-loading-status")
                            .semantics { liveRegion = LiveRegionMode.Polite },
                        color = palette.inkMuted,
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
            }
            if (state.historyLoading && !state.historyLoaded && state.historyErrorMessage == null) {
                item {
                    ApprovalLoadingSkeletons(testTag = "approval-history-loading-skeletons")
                }
            }
            state.historyErrorMessage?.let { message ->
                item {
                    TowerPanel(elevated = true) {
                        Text("Decision history unavailable", color = palette.ink, style = MaterialTheme.typography.titleMedium)
                        Text(message, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
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
        if (
            selectedSection == ApprovalInboxSection.NEEDS_REVIEW &&
            state.loading && state.approvals.isEmpty() && !state.hasError
        ) {
            item {
                ApprovalLoadingSkeletons(testTag = "approval-inbox-loading-skeletons")
            }
        }
        if (
            !state.hasError &&
            selectedSection == ApprovalInboxSection.NEEDS_REVIEW &&
            !state.loading &&
            !state.refreshing &&
            state.lastSuccessfulSyncAt != null &&
            visibleApprovals.isEmpty()
        ) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "You’re all caught up",
                        modifier = Modifier.semantics { heading() },
                        color = palette.ink,
                        style = MaterialTheme.typography.titleLarge
                    )
                    Text(
                        text = "New approval requests will appear here when an agent needs your decision.",
                        color = palette.inkMuted,
                        style = MaterialTheme.typography.bodyMedium
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    TextButton(onClick = onOpenConnect, modifier = Modifier.fillMaxWidth()) {
                        Text("Connect or manage agents")
                    }
                }
            }
        }

        if (selectedSection == ApprovalInboxSection.HISTORY &&
            !state.historyLoading && state.historyErrorMessage == null &&
            state.historyLoaded && visibleHistory.isEmpty()
        ) {
            item {
                TowerPanel(elevated = true) {
                    if (selectedHistoryFilter == ApprovalHistoryFilter.ALL) {
                        Text(
                            "No decisions recorded",
                            modifier = Modifier.semantics { heading() },
                            color = palette.ink,
                            style = MaterialTheme.typography.titleLarge
                        )
                        Text(
                            "Recorded approve, deny, and revision decisions will appear here.",
                            color = palette.inkMuted,
                            style = MaterialTheme.typography.bodyMedium
                        )
                    } else {
                        val filterName = when (selectedHistoryFilter) {
                            ApprovalHistoryFilter.ALL -> "decisions"
                            ApprovalHistoryFilter.APPROVED -> "approved decisions"
                            ApprovalHistoryFilter.REJECTED -> "denied decisions"
                            ApprovalHistoryFilter.REVISION_REQUESTED -> "revision requests"
                        }
                        Text(
                            "No $filterName match",
                            modifier = Modifier.semantics { heading() },
                            color = palette.ink,
                            style = MaterialTheme.typography.titleLarge
                        )
                        Text(
                            "Try another filter or show all recorded decisions.",
                            color = palette.inkMuted,
                            style = MaterialTheme.typography.bodyMedium
                        )
                        TextButton(onClick = { selectedHistoryFilter = ApprovalHistoryFilter.ALL }) {
                            Text("Show all decisions")
                        }
                    }
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
                        fontSize = MaterialTheme.typography.labelLarge.fontSize,
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
                        style = MaterialTheme.typography.bodyMedium
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

        if (selectedSection == ApprovalInboxSection.HISTORY && !state.hasError && terminalRequests.isNotEmpty()) {
            item {
                Text(
                    text = "Closed requests",
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("approval-terminal-history-heading")
                        .semantics { heading() },
                    color = palette.ink,
                    style = MaterialTheme.typography.titleLarge
                )
                Text(
                    text = "Expired and cancelled requests are shown for reference. They cannot be approved or denied.",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodyMedium
                )
            }
            items(terminalRequests, key = { "terminal-${it.id}" }) { approval ->
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

        if (selectedSection == ApprovalInboxSection.HISTORY && state.historyLoaded && state.historyHasMore) {
            item {
                Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)) {
                    state.historyLoadMoreError?.let { message ->
                        Text(
                            text = message,
                            color = palette.danger,
                            style = MaterialTheme.typography.bodySmall,
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

/** Quiet, static placeholders that preserve the approximate shape of approval cards. */
@Composable
private fun ApprovalLoadingSkeletons(testTag: String) {
    val palette = LocalTowerPalette.current
    val placeholder = palette.surfaceSubtle
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .testTag(testTag),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.md)
    ) {
        repeat(2) { index ->
            TowerPanel(elevated = true) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween
                ) {
                    SkeletonBlock(Modifier.fillMaxWidth(0.38f).height(14.dp), placeholder)
                    SkeletonBlock(Modifier.fillMaxWidth(0.2f).height(14.dp), placeholder)
                }
                Spacer(Modifier.height(TowerSpacing.md))
                SkeletonBlock(Modifier.fillMaxWidth(if (index == 0) 0.78f else 0.62f).height(20.dp), placeholder)
                Spacer(Modifier.height(TowerSpacing.sm))
                SkeletonBlock(Modifier.fillMaxWidth().height(14.dp), placeholder)
                Spacer(Modifier.height(TowerSpacing.xs))
                SkeletonBlock(Modifier.fillMaxWidth(0.72f).height(14.dp), placeholder)
                Spacer(Modifier.height(TowerSpacing.md))
                SkeletonBlock(Modifier.fillMaxWidth(0.34f).height(36.dp), placeholder)
            }
        }
    }
}

@Composable
private fun SkeletonBlock(modifier: Modifier, color: androidx.compose.ui.graphics.Color) {
    Box(modifier.clip(RoundedCornerShape(6.dp)).background(color))
}

@Composable
internal fun ApprovalDeliveryStatusContent(
    deliveryStatus: ApprovalDeliveryStatus?,
    loading: Boolean = false
) {
    val palette = LocalTowerPalette.current
    Text(
        "Delivery status",
        color = palette.ink,
        style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.SemiBold)
    )
    val (deliveryLabel, deliveryExplanation, deliveryColor) = when {
        loading -> Triple(
            "Checking delivery status…",
            "The decision is recorded. The app is checking whether the connector claimed it.",
            palette.caution
        )
        deliveryStatus == null -> Triple(
            "Delivery status unknown",
            "We could not match this decision to delivery evidence. Connector receipt and agent resumption are unconfirmed.",
            palette.caution
        )
        else -> when (deliveryStatus) {
            ApprovalDeliveryStatus.NOT_CONFIRMED -> Triple(
                "Delivery not confirmed",
                "The server has no usable delivery evidence for this decision.",
                palette.caution
            )
            ApprovalDeliveryStatus.NOT_APPLICABLE -> Triple(
                "No delivery expected",
                "This decision does not authorize an external agent to resume.",
                palette.inkMuted
            )
            ApprovalDeliveryStatus.AWAITING_DELIVERY -> Triple(
                "Awaiting agent delivery",
                "The approval is still within its delivery window. The connector has not claimed it yet.",
                palette.caution
            )
            ApprovalDeliveryStatus.EXPIRED -> Triple(
                "Approval expired before delivery",
                "The delivery window elapsed before the connector claimed this decision.",
                palette.danger
            )
            ApprovalDeliveryStatus.CONSUMED -> Triple(
                "Decision consumed",
                "The connector claimed this decision once. This confirms protocol consumption, not agent execution or task completion.",
                palette.info
            )
            ApprovalDeliveryStatus.DELIVERY_UNCONFIRMED -> Triple(
                "Delivery response ambiguous",
                "The connector reported an ambiguous claim response. The server may have recorded the claim; connector receipt and agent execution or resumption remain unconfirmed.",
                palette.danger
            )
        }
    }
    StatusBadge(
        deliveryLabel,
        modifier = Modifier.testTag("approval-delivery-status-label"),
        color = deliveryColor,
        fontSize = MaterialTheme.typography.labelLarge.fontSize,
        uppercase = false
    )
    Text(deliveryExplanation, color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
}

@Composable
private fun ApprovalDecisionHistoryCard(event: ApprovalHistoryEvent) {
    val palette = LocalTowerPalette.current
    val decisionLabel = event.decision.let(::decisionHistoryLabel)
    TowerPanel(elevated = true) {
        StatusBadge(
            decisionLabel,
            modifier = Modifier.semantics(mergeDescendants = true) {
                contentDescription = "Decision status: $decisionLabel"
            },
            color = when (event.decision.trim().lowercase()) {
                "approve", "approved" -> palette.positive
                "reject", "rejected", "deny", "denied" -> palette.danger
                else -> palette.caution
            },
            fontSize = MaterialTheme.typography.labelLarge.fontSize,
            uppercase = false
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        ApprovalDeliveryStatusContent(deliveryStatus = event.deliveryStatus)
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text("Decided ${formatHistoryTime(event.decidedAt)}", color = palette.inkMuted, style = MaterialTheme.typography.bodySmall)
        Text(
            "Reviewer · ${event.reviewerLabel}",
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.testTag("approval-history-reviewer")
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("Reason", color = palette.ink, style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.SemiBold))
        Text(event.reason ?: "No reason was provided.", color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        val snapshot = event.requestSnapshot.takeIf { event.requestSnapshotVerified }
        if (snapshot == null) {
            Text("Request details unavailable", color = palette.ink, style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.SemiBold))
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
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(palette.surfaceSubtle.copy(alpha = 0.48f))
            .padding(TowerSpacing.md),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
    ) {
        Text("Verified request", color = palette.ink, style = MaterialTheme.typography.labelLarge)
        Text(
            snapshot.title?.takeIf(String::isNotBlank) ?: "Untitled request",
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium,
            modifier = Modifier.testTag("approval-history-request-title")
        )
        Text(snapshot.summary?.takeIf(String::isNotBlank) ?: "Request summary unavailable", color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        val provider = listOfNotNull(
            snapshot.sourceAgentName?.takeIf(String::isNotBlank) ?: snapshot.agentName?.takeIf(String::isNotBlank),
            snapshot.sourceProvider?.takeIf(String::isNotBlank)
        ).distinct().joinToString(" · ")
        if (provider.isNotBlank()) Text(provider, color = palette.inkMuted, style = MaterialTheme.typography.bodySmall)
        val risk = snapshot.riskLevel.name.lowercase().replaceFirstChar { it.uppercase() }
        StatusBadge(
            if (snapshot.riskLevel == RiskLevel.UNKNOWN) "Risk unavailable" else "$risk risk",
            color = if (snapshot.riskLevel == RiskLevel.UNKNOWN) palette.caution else palette.brand,
            fontSize = MaterialTheme.typography.labelLarge.fontSize,
            uppercase = false
        )
        snapshot.targetBranch?.takeIf(String::isNotBlank)?.let { branch ->
            Text("Target branch · $branch", color = palette.inkMuted, style = MaterialTheme.typography.bodySmall)
        }
        if (snapshot.files.isNotEmpty()) {
            Text("Affected files", color = palette.ink, style = MaterialTheme.typography.labelLarge)
            snapshot.files.forEach { file -> Text(file.path, color = palette.inkMuted, style = MaterialTheme.typography.bodySmall) }
        }
        if (snapshot.testPlan.isNotEmpty()) {
            Text("Test plan", color = palette.ink, style = MaterialTheme.typography.labelLarge)
            snapshot.testPlan.forEach { Text(it, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium) }
        }
        snapshot.rollbackPlan?.takeIf(String::isNotBlank)?.let { plan ->
            Text("Rollback plan", color = palette.ink, style = MaterialTheme.typography.labelLarge)
            Text(plan, color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

internal fun formatHistoryTime(raw: String?): String {
    if (raw.isNullOrBlank()) return "time unavailable"
    return runCatching {
        // Preserve the user's local presentation while making the offset explicit in
        // the audit/history record, which may be reviewed across time zones.
        val formatter = DateTimeFormatter.ofPattern("MMM d, yyyy 'at' h:mm a XXX", Locale.getDefault())
            .withZone(ZoneId.systemDefault())
        formatter.format(Instant.parse(raw))
    }.getOrDefault("time unavailable")
}
