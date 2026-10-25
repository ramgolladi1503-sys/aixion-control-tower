package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Checkbox
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.time.Duration
import java.time.Instant
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalHistoryEvent
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.DiffBlock
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.SourceBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.components.formatApprovalExpiry
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.towerScreenHorizontalPadding

@Composable
fun ApprovalDetailScreen(
    viewModel: ApprovalsViewModel = viewModel(),
    onOpenDiff: () -> Unit = {},
    onDecisionSubmitted: () -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    val approval = state.selectedApproval

    if (approval == null) {
        val detailMessage = state.lastActionMessage?.takeIf {
            it.startsWith("Failed to open approval") &&
                (state.lastActionApprovalId == state.detailTargetApprovalId || state.detailTargetApprovalId == null)
        } ?: state.errorMessage
        val retryTarget = state.detailTargetApprovalId?.takeIf { state.openingApprovalId == null }
        ApprovalDetailEmptyContent(
            loading = state.openingApprovalId != null,
            message = detailMessage,
            onRetry = retryTarget?.let { targetId -> { viewModel.openApprovalById(targetId) } }
        )
        return
    }

    ApprovalDetailContent(
        approval = approval,
        lastActionMessage = state.lastActionMessage,
        lastActionApprovalId = state.lastActionApprovalId,
        decisionInFlight = state.decisionInFlight,
        decisionNeedsReconciliation = state.decisionNeedsReconciliation,
        decisionOutcomeApprovalId = state.decisionOutcomeApprovalId,
        decisionOutcomeDecision = state.decisionOutcomeDecision,
        decisionOutcomeEvent = state.decisionOutcomeEvent,
        decisionDeliveryLoading = state.decisionDeliveryLoading,
        onReconcileDecision = viewModel::reconcileDecisionStatus,
        onOpenDiff = onOpenDiff,
        onApprove = {
            viewModel.decide(
                decision = "approve",
                reason = "Approved from mobile detail review.",
                onCompleted = onDecisionSubmitted
            )
        },
        onReject = {
            viewModel.decide(
                decision = "reject",
                reason = "Denied from mobile detail review.",
                onCompleted = onDecisionSubmitted
            )
        },
    )
}

@Composable
internal fun ApprovalDetailEmptyContent(
    loading: Boolean,
    message: String?,
    onRetry: (() -> Unit)?
) {
    val palette = LocalTowerPalette.current
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.Center
    ) {
        TowerPanel(elevated = true) {
            when {
                loading -> {
                    Text(
                        "Loading requested approval…",
                        modifier = Modifier.testTag("approval-detail-loading"),
                        color = palette.ink,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text("The app is fetching this approval from the authenticated backend.", color = palette.inkMuted, fontSize = 14.sp)
                }
                message != null -> {
                    Text("Approval unavailable", color = palette.ink, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(message, modifier = Modifier.testTag("approval-detail-open-error"), color = palette.inkMuted, fontSize = 14.sp)
                    onRetry?.let { retry ->
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        OutlinedButton(onClick = retry, modifier = Modifier.fillMaxWidth()) {
                            Text("Retry approval load")
                        }
                    }
                }
                else -> {
                    Text("No approval selected", color = palette.ink, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text("Open the inbox and select a request to review.", color = palette.inkMuted, fontSize = 14.sp)
                }
            }
        }
    }
}

@Composable
fun ApprovalDetailContent(
    approval: ApprovalSummary,
    lastActionMessage: String?,
    onOpenDiff: () -> Unit,
    onApprove: () -> Unit,
    onReject: () -> Unit,
    lastActionApprovalId: String? = null,
    decisionInFlight: Boolean = false,
    decisionNeedsReconciliation: Boolean = false,
    decisionOutcomeApprovalId: String? = null,
    decisionOutcomeDecision: String? = null,
    decisionOutcomeEvent: ApprovalHistoryEvent? = null,
    decisionDeliveryLoading: Boolean = false,
    onReconcileDecision: () -> Unit = {}
) {
    val palette = LocalTowerPalette.current
    val requestListState = rememberLazyListState()
    val coroutineScope = rememberCoroutineScope()
    var decisionClock by remember(approval.id) { mutableStateOf(Instant.now()) }
    val expiryInstant = remember(approval.expiresAt) {
        ApprovalDecisionPolicy.expiryInstant(approval.expiresAt)
    }
    LaunchedEffect(approval.id, approval.expiresAt) {
        while (true) {
            val now = Instant.now()
            decisionClock = now
            val expiry = expiryInstant ?: break
            val remainingMillis = Duration.between(now, expiry).toMillis()
            if (remainingMillis <= 0) break
            delay(minOf(remainingMillis, 1_000L))
        }
    }
    val decisionBlockReason = ApprovalDecisionPolicy.blockReason(approval, decisionClock)
    val hasUnsupportedData = approval.status == ApprovalStatus.UNKNOWN || approval.risk == RiskLevel.UNKNOWN
    val isBlocked = approval.status == ApprovalStatus.BLOCKED || approval.risk == RiskLevel.BLOCKED
    val canDecide = !decisionInFlight && !decisionNeedsReconciliation &&
        approval.status in setOf(ApprovalStatus.REQUESTED, ApprovalStatus.PENDING_REVIEW) &&
        !isBlocked && !hasUnsupportedData && decisionBlockReason == null
    val hasRequiredActions = approval.requiredActions.isNotEmpty()
    val hasExactToolRequest = approval.hasExactToolRequestPayload
    // Acknowledgement applies to the entire reviewed snapshot, including provider and
    // invocation identity. If the backend rebinds an approval to another invocation,
    // the reviewer must inspect the request context again before approving.
    var exactToolRequestReviewed by remember(approval) {
        mutableStateOf(false)
    }
    val requiresFullDiffReview = requiresExplicitRiskConfirmation(approval.risk) &&
        !hasExactToolRequest && approval.files.isNotEmpty()
    var fullDiffOpened by rememberSaveable(approval.id, approval.files, approval.risk, approval.status) {
        mutableStateOf(false)
    }
    var fullDiffReviewed by rememberSaveable(approval.id, approval.files, approval.risk, approval.status) {
        mutableStateOf(false)
    }
    val canApprove = canDecide && !hasRequiredActions &&
        (!hasExactToolRequest || exactToolRequestReviewed) &&
        (!requiresFullDiffReview || (fullDiffOpened && fullDiffReviewed))
    val requiresRiskConfirmation = requiresExplicitRiskConfirmation(approval.risk)
    var showRiskConfirmation by remember(approval.id) { mutableStateOf(false) }
    var showCompactToolReview by remember(approval.id) { mutableStateOf(false) }
    var showTechnicalDetails by remember(approval.id) { mutableStateOf(false) }

    BoxWithConstraints(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
    ) {
        // Keep decision controls outside the request scroll surface at every height.
        // The compact panel below reduces supporting copy so it remains usable in
        // landscape, split-screen, and large-text configurations.
        val compactViewport = maxHeight < 500.dp
        Column(modifier = Modifier.fillMaxSize()) {
            LazyColumn(
                state = requestListState,
                modifier = Modifier
                    .testTag("approval-detail-list")
                    .weight(1f)
                    .fillMaxWidth()
                    .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
            ) {
                if (hasExactToolRequest) {
                    item {
                        ExactToolRequestHeader(approval)
                    }
                    items(approval.files) { file -> DiffBlock(file = file) }
                    item {
                        ExactToolDecisionContext(approval = approval)
                    }
                    item {
                        ExactToolRequestReviewAcknowledgement(
                            checked = exactToolRequestReviewed,
                            onCheckedChange = { exactToolRequestReviewed = it }
                        )
                    }
                }

                item {
                    ApprovalHeroPanel(
                        approval = approval,
                        canApprove = canApprove,
                        canDecide = canDecide,
                        isBlocked = isBlocked,
                        hasUnsupportedData = hasUnsupportedData,
                        hasRequiredActions = hasRequiredActions,
                        showDecisionContext = !hasExactToolRequest
                    )
                }

                if (lastActionMessage != null && lastActionApprovalId == approval.id) {
                    item { DetailPanel(title = "Last Action", body = lastActionMessage) }
                }

                if (decisionOutcomeApprovalId == approval.id) {
                    item {
                        DecisionOutcomePanel(
                            decision = decisionOutcomeDecision,
                            event = decisionOutcomeEvent,
                            deliveryLoading = decisionDeliveryLoading
                        )
                    }
                }

                if (hasUnsupportedData) {
                    item {
                        WarningPanel(
                            title = "Unsupported approval data",
                            body = "The backend returned a status or risk value this app does not recognize. Decisions are disabled until the app and backend versions are compatible."
                        )
                    }
                } else if (isBlocked) {
                    item {
                        WarningPanel(
                            title = "Approval blocked",
                            body = "This request violates a hard safety rule. It cannot be approved from mobile."
                        )
                    }
                } else if (decisionBlockReason != null) {
                    item {
                        WarningPanel(
                            title = "Decision blocked",
                            body = decisionBlockReason
                        )
                    }
                } else if (!canDecide && !decisionNeedsReconciliation && !decisionInFlight) {
                    val terminalCopy = when (approval.status) {
                        ApprovalStatus.EXPIRED -> "This request expired and can no longer be approved or denied."
                        ApprovalStatus.CANCELLED -> "This request was cancelled and can no longer be approved or denied."
                        ApprovalStatus.DENIED, ApprovalStatus.REJECTED ->
                            "This request was denied. Its decision cannot be changed."
                        else -> "This approval is ${approval.status.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() }}. Decisions are available only while it is pending."
                    }
                    item {
                        WarningPanel(
                            title = when (approval.status) {
                                ApprovalStatus.EXPIRED -> "Request expired"
                                ApprovalStatus.CANCELLED -> "Request cancelled"
                                ApprovalStatus.DENIED, ApprovalStatus.REJECTED -> "Request denied"
                                else -> "Decision closed"
                            },
                            body = terminalCopy
                        )
                    }
                }

                if (hasRequiredActions) {
                    item { RequiredActionsPanel(actions = approval.requiredActions) }
                }

                item {
                    TowerSectionHeader(
                        title = "Review before deciding",
                        subtitle = "Check the risk rationale, test plan, and rollback plan before making a decision."
                    )
                }

                item {
                    DetailPanel(
                        title = "Risk rationale",
                        body = approval.riskReasons.ifEmpty {
                            listOf("No risk rationale was supplied by the backend.")
                        }.joinToString("\n")
                    )
                }

                item {
                    TechnicalDetailsDisclosure(
                        approval = approval,
                        expanded = showTechnicalDetails,
                        onToggle = { showTechnicalDetails = !showTechnicalDetails }
                    )
                }
                item { DetailPanel(title = "Test Plan", body = approval.testPlan.ifEmpty { listOf("No tests listed") }.joinToString("\n")) }
                item { DetailPanel(title = "Rollback Plan", body = approval.rollbackPlan.ifBlank { "No rollback plan provided" }) }

                item {
                    Button(
                        onClick = {
                            if (requiresFullDiffReview) {
                                fullDiffOpened = true
                                fullDiffReviewed = false
                            }
                            onOpenDiff()
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = palette.brand, contentColor = palette.canvas),
                        modifier = Modifier.fillMaxWidth().testTag("approval-full-diff-review")
                    ) { Text("Review Full Diff") }
                }

                if (!hasExactToolRequest) {
                    item {
                        TowerSectionHeader(
                            title = "File Changes",
                            subtitle = "Inline preview only. Open the full diff before approving risky changes.",
                            modifier = Modifier.testTag("approval-file-changes")
                        )
                    }

                    items(approval.files) { file -> DiffBlock(file = file) }
                }

                if (requiresFullDiffReview) {
                    item {
                        FullDiffReviewAcknowledgement(
                            opened = fullDiffOpened,
                            checked = fullDiffReviewed,
                            onCheckedChange = { fullDiffReviewed = it }
                        )
                    }
                }

            }

            if (canDecide || decisionInFlight || decisionNeedsReconciliation ||
                isBlocked || hasUnsupportedData || decisionBlockReason != null
            ) {
                DecisionPanel(
                    canApprove = canApprove,
                    exactToolRequestNeedsReview = hasExactToolRequest && !exactToolRequestReviewed,
                    fullDiffReviewNeedsReview = requiresFullDiffReview && (!fullDiffOpened || !fullDiffReviewed),
                    fullDiffOpened = fullDiffOpened,
                    canDecide = canDecide,
                    isBlocked = isBlocked,
                    hasUnsupportedData = hasUnsupportedData,
                    decisionBlockReason = decisionBlockReason,
                    decisionInFlight = decisionInFlight,
                    decisionNeedsReconciliation = decisionNeedsReconciliation,
                    onReconcileDecision = onReconcileDecision,
                    approvalTitle = approval.title,
                    approvalId = approval.id,
                    approvalProvider = approval.sourceProvider.ifBlank { approval.sourceLabel },
                    approvalRisk = approval.risk,
                    expiresAt = approval.expiresAt,
                    now = decisionClock,
                    onApprove = {
                        if (requiresRiskConfirmation) {
                            showRiskConfirmation = true
                        } else {
                            onApprove()
                        }
                    },
                    onReject = onReject,
                    onReviewFullRequest = {
                        if (compactViewport && hasExactToolRequest) {
                            showCompactToolReview = true
                        } else {
                            coroutineScope.launch { requestListState.animateScrollToItem(0) }
                        }
                    },
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.sm)
                        .testTag("approval-decision-panel"),
                    compactLayout = compactViewport,
                    showDisabledActions = compactViewport && (
                        isBlocked || hasUnsupportedData || decisionBlockReason != null ||
                            decisionNeedsReconciliation
                        )
                )
            }
        }
    }

    if (showCompactToolReview && hasExactToolRequest) {
        CompactExactToolRequestReviewDialog(
            approval = approval,
            checked = exactToolRequestReviewed,
            canConfirm = canDecide,
            onConfirm = {
                exactToolRequestReviewed = true
                showCompactToolReview = false
            },
            onDismiss = { showCompactToolReview = false }
        )
    }

    if (showRiskConfirmation) {
        AlertDialog(
            onDismissRequest = { showRiskConfirmation = false },
            title = { Text("Confirm high-risk approval") },
            text = {
                Column(
                    modifier = Modifier
                        .testTag("approval-high-risk-confirmation")
                        .heightIn(max = 360.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    Text("Action: ${approval.title}\n${approval.summary}", style = MaterialTheme.typography.bodyLarge)
                    if (hasExactToolRequest) {
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        Text("Exact requested tool action", fontWeight = FontWeight.SemiBold)
                        approval.files.forEach { file ->
                            Spacer(modifier = Modifier.height(TowerSpacing.xs))
                            Text(
                                file.path,
                                modifier = Modifier.testTag("approval-confirmation-tool-path"),
                                fontWeight = FontWeight.Medium
                            )
                            Text(
                                file.diff,
                                modifier = Modifier.testTag("approval-confirmation-tool-arguments"),
                                fontFamily = FontFamily.Monospace
                            )
                        }
                    }
                    Spacer(modifier = Modifier.height(TowerSpacing.md))
                    Text(
                        buildString {
                            append("Provider: ${approvalProviderName(approval.sourceProvider)}\n")
                            append("Source verification: ${if (approval.verifiedSource) "Verified" else "Not verified"}\n")
                            append("Request ID: ${shortSafeApprovalId(approval.id)}\n")
                            append("Invocation ID: ${approval.sourceInvocationId?.let(::shortSafeApprovalId) ?: "Not provided"}\n")
                            append("Target: ${approval.projectName.ifBlank { "Not provided" }} · ${approval.targetBranch.ifBlank { "Not provided" }}\n")
                            append("Risk: ${approval.risk.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() }}")
                        },
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
            },
            confirmButton = {
                Button(
                    onClick = {
                        showRiskConfirmation = false
                        onApprove()
                    },
                    enabled = canApprove && !decisionInFlight,
                    modifier = Modifier.heightIn(min = 48.dp)
                ) { Text("Approve this request") }
            },
            dismissButton = {
                OutlinedButton(
                    onClick = { showRiskConfirmation = false },
                    modifier = Modifier.heightIn(min = 48.dp)
                ) { Text("Go back") }
            }
        )
    }
}

@Composable
private fun CompactExactToolRequestReviewDialog(
    approval: ApprovalSummary,
    checked: Boolean,
    canConfirm: Boolean,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    val palette = LocalTowerPalette.current
    var draftChecked by remember(approval.id) { mutableStateOf(checked) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Review full request") },
        text = {
            Column(
                modifier = Modifier
                    .heightIn(max = 360.dp)
                    .verticalScroll(rememberScrollState())
                    .testTag("approval-compact-tool-review-content"),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
            ) {
                Text("Action: ${approval.title}", color = palette.ink, style = MaterialTheme.typography.titleSmall)
                Text(
                    "Provider: ${approvalProviderName(approval.sourceProvider)} · Request ${shortSafeApprovalId(approval.id)}",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.bodyMedium
                )
                Text("Project: ${approval.projectName.ifBlank { "Not provided" }}", color = palette.inkMuted)
                Text(
                    "Invocation: ${approval.sourceInvocationId?.let(::shortSafeApprovalId) ?: "Not provided"}",
                    color = palette.inkMuted
                )
                Text("Exact tool name and arguments", color = palette.ink, style = MaterialTheme.typography.titleSmall)
                approval.files.forEachIndexed { index, file ->
                    DiffBlock(
                        file = file,
                        modifier = Modifier.testTag("approval-compact-tool-arguments-$index")
                    )
                }
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("approval-compact-tool-review-acknowledgement")
                        .toggleable(
                            value = draftChecked,
                            role = Role.Checkbox,
                            onValueChange = { draftChecked = it }
                        )
                        .padding(vertical = TowerSpacing.sm),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Checkbox(checked = draftChecked, onCheckedChange = null)
                    Text(
                        "I reviewed the exact tool name and arguments above.",
                        color = palette.ink,
                        style = MaterialTheme.typography.bodyMedium
                    )
                }
            }
        },
        confirmButton = {
            Button(onClick = onConfirm, enabled = draftChecked && canConfirm) {
                Text("Acknowledge and continue")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Close") }
        }
    )
}

@Composable
private fun DecisionOutcomePanel(
    decision: String?,
    event: ApprovalHistoryEvent?,
    deliveryLoading: Boolean
) {
    val palette = LocalTowerPalette.current
    val decisionLabel = when (normalizeApprovalDecisionForHistory(decision.orEmpty())) {
        "approve" -> "Approved"
        "reject" -> "Denied"
        "revise" -> "Revision requested"
        else -> "Decision recorded"
    }
    TowerPanel(
        modifier = Modifier
            .testTag("approval-decision-outcome-panel")
            .semantics { liveRegion = LiveRegionMode.Polite },
        elevated = true
    ) {
        Text(
            "Decision recorded",
            modifier = Modifier.testTag("approval-decision-outcome"),
            color = palette.ink,
            style = MaterialTheme.typography.titleMedium
        )
        val serverDecisionLabel = event?.let {
            when (normalizeApprovalDecisionForHistory(it.decision)) {
                "approve" -> "Approved"
                "reject" -> "Denied"
                "revise" -> "Revision requested"
                else -> "Decision recorded"
            }
        } ?: decisionLabel
        Text("Server decision: $serverDecisionLabel", color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
        Text(
            "Request ${shortSafeApprovalId(event?.approvalId ?: "")}" +
                (event?.eventId?.let { " · Event ${shortSafeApprovalId(it)}" } ?: ""),
            modifier = Modifier.testTag("approval-decision-correlation"),
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Text("Recorded ${formatHistoryTime(event?.decidedAt)}", color = palette.inkMuted, style = MaterialTheme.typography.bodyMedium)
        ApprovalDeliveryStatusContent(
            deliveryStatus = event?.deliveryStatus,
            loading = deliveryLoading
        )
    }
}

/** Abbreviate opaque identifiers so the dialog identifies the request without exposing raw IDs. */
internal fun shortSafeApprovalId(raw: String): String {
    val safeCharacters = raw.filter { it.isLetterOrDigit() || it == '-' || it == '_' }
    return safeCharacters.takeLast(8).ifBlank { "unavailable" }
}

@Composable
private fun ExactToolRequestHeader(approval: ApprovalSummary) {
    val palette = LocalTowerPalette.current
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("approval-exact-tool-request"),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
    ) {
        Text(
            text = "Exact requested tool action",
            color = palette.ink,
            fontSize = 18.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 24.sp
        )
        SourceBadge(
            approval = approval,
            modifier = Modifier.testTag("approval-detail-source-trust")
        )
        Text(
            text = "Provider: ${approvalProviderName(approval.sourceProvider)} · Project: ${approval.projectName.ifBlank { "Not provided" }}",
            modifier = Modifier.testTag("approval-exact-tool-context"),
            color = palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium
        )
        Text(
            text = "Review the tool name and arguments below before deciding.",
            color = palette.inkMuted,
            fontSize = 14.sp,
            lineHeight = 20.sp
        )
    }
}

private fun approvalProviderName(provider: String): String = when {
    provider.equals("ANTIGRAVITY", ignoreCase = true) -> "Antigravity"
    provider.equals("CODEX", ignoreCase = true) -> "Codex"
    provider.equals("OTHER", ignoreCase = true) -> "Other"
    provider.equals("MANUAL", ignoreCase = true) -> "Manual"
    else -> provider.ifBlank { "Unknown" }
}

@Composable
private fun ExactToolRequestReviewAcknowledgement(
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit
) {
    val palette = LocalTowerPalette.current
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("approval-exact-tool-review-acknowledgement")
            .toggleable(
                value = checked,
                role = Role.Checkbox,
                onValueChange = onCheckedChange
            )
            .padding(vertical = TowerSpacing.sm),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Checkbox(
            checked = checked,
            onCheckedChange = null,
            modifier = Modifier.testTag("approval-exact-tool-reviewed")
        )
        Text(
            text = "I reviewed the exact tool name and arguments above.",
            color = palette.ink,
            fontSize = 14.sp,
            lineHeight = 20.sp
        )
    }
}

@Composable
private fun FullDiffReviewAcknowledgement(
    opened: Boolean,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit
) {
    val palette = LocalTowerPalette.current
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .testTag("approval-full-diff-review-acknowledgement")
            .toggleable(
                value = checked,
                enabled = opened,
                role = Role.Checkbox,
                onValueChange = onCheckedChange
            )
            .padding(vertical = TowerSpacing.sm),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Checkbox(
            checked = checked,
            onCheckedChange = null,
            enabled = opened,
            modifier = Modifier.testTag("approval-full-diff-reviewed")
        )
        Text(
            text = if (opened) "I reviewed the full file diff above." else "Open the full diff before acknowledging review.",
            color = palette.ink,
            fontSize = 14.sp,
            lineHeight = 20.sp
        )
    }
}

internal fun requiresExplicitRiskConfirmation(risk: RiskLevel): Boolean =
    risk == RiskLevel.HIGH || risk == RiskLevel.CRITICAL

@Composable
private fun TechnicalDetailsDisclosure(
    approval: ApprovalSummary,
    expanded: Boolean,
    onToggle: () -> Unit
) {
    val palette = LocalTowerPalette.current
    TowerPanel(elevated = true) {
        TextButton(onClick = onToggle, modifier = Modifier.fillMaxWidth()) {
            Text(if (expanded) "Hide technical details" else "Show technical details")
        }
        if (expanded) {
            Text("Request and connector identifiers", color = palette.ink, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
            Text(
                buildList {
                    add("Approval ID: ${approval.id}")
                    add("Project: ${approval.projectName}")
                    if (approval.projectId.isNotBlank()) add("Project ID: ${approval.projectId}")
                    add("Agent: ${approval.agentName}")
                    add("Provider: ${approval.sourceProvider}")
                    add("Source agent ID: ${approval.sourceAgentId ?: "Not provided"}")
                    add("Session ID: ${approval.sourceSessionId ?: "Not provided"}")
                    add("Source invocation ID: ${approval.sourceInvocationId ?: "Not provided"}")
                    add("Expires at: ${approval.expiresAt ?: "Not provided"}")
                    add("Source verified: ${if (approval.verifiedSource) "Yes" else "No"}")
                }.joinToString("\n"),
                color = palette.inkMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
        }
    }
}

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun ApprovalHeroPanel(
    approval: ApprovalSummary,
    canApprove: Boolean,
    canDecide: Boolean,
    isBlocked: Boolean,
    hasUnsupportedData: Boolean,
    hasRequiredActions: Boolean,
    showDecisionContext: Boolean
) {
    val palette = LocalTowerPalette.current
    TowerHeroPanel {
        Column(modifier = Modifier.fillMaxWidth()) {
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                StatusBadge(
                    label = when {
                        hasUnsupportedData -> "Unsupported data"
                        isBlocked -> "Blocked"
                        !canDecide -> "Decision closed"
                        canApprove -> "Ready for decision"
                        hasRequiredActions -> "Safety gate"
                        else -> "Review required"
                    },
                    color = when {
                        isBlocked || hasUnsupportedData -> palette.danger
                        canApprove -> palette.positive
                        else -> palette.caution
                    },
                    fontSize = 14.sp,
                    uppercase = false
                )
                if (showDecisionContext) {
                    SourceBadge(
                        approval = approval,
                        modifier = Modifier.testTag("approval-detail-source-trust")
                    )
                }
                if (showDecisionContext) {
                    RiskBadge(approval.risk, readable = true)
                }
            }
            Spacer(modifier = Modifier.height(TowerSpacing.md))
            Text(
                text = approval.title,
                color = palette.ink,
                fontSize = 25.sp,
                fontWeight = FontWeight.SemiBold,
                lineHeight = 30.sp
            )
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Text(text = approval.summary, color = palette.inkMuted, style = MaterialTheme.typography.bodyLarge)
        }

        if (showDecisionContext) {
            Spacer(modifier = Modifier.height(TowerSpacing.md))
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(palette.canvas.copy(alpha = 0.42f), RoundedCornerShape(16.dp))
                    .padding(TowerSpacing.md),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
            ) {
                Text("Request at a glance", color = palette.ink, style = MaterialTheme.typography.titleSmall)
                DecisionContextLine("Project", approval.projectName.ifBlank { "Not provided" })
                DecisionContextLine("Target", approval.targetBranch.ifBlank { "Not provided" })
                DecisionContextLine("Provider", "${approval.sourceLabel} · ${if (approval.verifiedSource) "Verified source" else "Source not verified"}")
                DecisionContextLine("Session", approval.sourceSessionId?.let(::shortSafeApprovalId) ?: "Not provided")
                DecisionContextLine("Invocation", approval.sourceInvocationId?.let(::shortSafeApprovalId) ?: "Not provided")
                DecisionContextLine("Expires", formatApprovalExpiry(approval.expiresAt))
            }
        }
    }
}

@Composable
private fun ExactToolDecisionContext(
    approval: ApprovalSummary,
    modifier: Modifier = Modifier
) {
    val palette = LocalTowerPalette.current
    TowerPanel(modifier = modifier.testTag("approval-exact-tool-decision-context"), elevated = true) {
        Text("Before you decide", color = palette.ink, style = MaterialTheme.typography.titleSmall)
        DecisionContextLine("Target", approval.targetBranch.ifBlank { "Not provided" })
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Risk", color = palette.inkMuted, style = MaterialTheme.typography.labelLarge)
            RiskBadge(approval.risk, readable = true)
        }
        DecisionContextLine(
            "Why flagged",
            approval.riskReasons
                .map(String::trim)
                .filter(String::isNotEmpty)
                .joinToString("; ")
                .ifBlank { "No risk rationale was supplied by the backend." }
        )
        DecisionContextLine("Expires", formatApprovalExpiry(approval.expiresAt))
        approval.sourceInvocationId
            ?.takeIf(String::isNotBlank)
            ?.let { DecisionContextLine("Invocation", shortSafeApprovalId(it)) }
    }
}

@Composable
private fun DecisionContextLine(label: String, value: String) {
    val palette = LocalTowerPalette.current
    val fontScale = LocalDensity.current.fontScale
    BoxWithConstraints(modifier = Modifier.fillMaxWidth()) {
        val stackLabelAndValue = shouldStackDecisionContext(maxWidth, fontScale)
        if (stackLabelAndValue) {
            Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)) {
                Text(
                    label,
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.labelLarge
                )
                Text(
                    value,
                    color = palette.ink,
                    style = MaterialTheme.typography.bodyMedium
                )
            }
        } else {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
                verticalAlignment = Alignment.Top
            ) {
                Text(
                    label,
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.labelLarge,
                    modifier = Modifier.width(96.dp)
                )
                Text(
                    value,
                    color = palette.ink,
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.weight(1f)
                )
            }
        }
    }
}

internal fun shouldStackDecisionContext(maxWidth: androidx.compose.ui.unit.Dp, fontScale: Float): Boolean =
    maxWidth < 360.dp || fontScale >= 1.5f

@Composable
private fun DecisionContextBody(label: String, value: String) {
    val palette = LocalTowerPalette.current
    Column(verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)) {
        Text(
            label,
            color = palette.inkMuted,
            style = MaterialTheme.typography.labelLarge
        )
        Text(value, color = palette.ink, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun DetailPanel(title: String, body: String, modifier: Modifier = Modifier) {
    val palette = LocalTowerPalette.current
    TowerPanel(modifier = modifier, elevated = true) {
        Text(
            title,
            color = palette.inkMuted,
            style = MaterialTheme.typography.labelLarge
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = palette.ink, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun WarningPanel(title: String, body: String) {
    val palette = LocalTowerPalette.current
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(palette.danger.copy(alpha = 0.12f), RoundedCornerShape(24.dp))
            .border(1.dp, palette.danger.copy(alpha = 0.32f), RoundedCornerShape(24.dp))
            .padding(16.dp)
    ) {
        Text(title, color = palette.danger, style = MaterialTheme.typography.titleSmall)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = palette.ink, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun RequiredActionsPanel(actions: List<String>) {
    val palette = LocalTowerPalette.current
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(palette.danger.copy(alpha = 0.10f), RoundedCornerShape(24.dp))
            .border(1.dp, palette.danger.copy(alpha = 0.28f), RoundedCornerShape(24.dp))
            .padding(16.dp)
    ) {
        Text("Required Before Approval", color = palette.danger, style = MaterialTheme.typography.titleSmall)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        actions.forEach { action -> Text("• $action", color = palette.ink, style = MaterialTheme.typography.bodyLarge) }
    }
}

@Composable
private fun DecisionPanel(
    canApprove: Boolean,
    exactToolRequestNeedsReview: Boolean,
    fullDiffReviewNeedsReview: Boolean,
    fullDiffOpened: Boolean,
    canDecide: Boolean,
    isBlocked: Boolean,
    hasUnsupportedData: Boolean,
    decisionBlockReason: String?,
    decisionInFlight: Boolean,
    decisionNeedsReconciliation: Boolean,
    onReconcileDecision: () -> Unit,
    approvalTitle: String,
    approvalId: String,
    approvalProvider: String,
    approvalRisk: RiskLevel,
    expiresAt: String?,
    now: Instant,
    onApprove: () -> Unit,
    onReject: () -> Unit,
    onReviewFullRequest: () -> Unit,
    modifier: Modifier = Modifier,
    compactLayout: Boolean = false,
    showDisabledActions: Boolean = false
) {
    val palette = LocalTowerPalette.current
    TowerPanel(modifier = modifier, elevated = true) {
        if (compactLayout) {
            TextButton(
                onClick = onReviewFullRequest,
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp)
                    .semantics(mergeDescendants = true) {
                        contentDescription = "Review request: $approvalTitle from ${approvalProviderName(approvalProvider)}, request ${shortSafeApprovalId(approvalId)}, risk ${approvalRisk.name.lowercase().replace('_', ' ')}."
                    }
                    .testTag("approval-decision-identity-title")
            ) {
                Text(
                    text = "Review request",
                    color = palette.brand,
                    style = MaterialTheme.typography.labelLarge
                )
            }
        } else {
            Text(
                text = approvalTitle,
                color = palette.ink,
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.testTag("approval-decision-identity-title")
            )
        }
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .semantics(mergeDescendants = true) {
                    contentDescription = "Decision context: $approvalProvider, $approvalTitle, request ${shortSafeApprovalId(approvalId)}, risk ${approvalRisk.name.lowercase().replace('_', ' ')}."
                }
                .testTag("approval-decision-identity"),
            horizontalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.xs)
            ) {
                Text(
                    text = approvalProvider,
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.labelMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.testTag("approval-decision-identity-provider")
                )
                Text(
                    text = "Request ${shortSafeApprovalId(approvalId)}",
                    color = palette.inkMuted,
                    style = MaterialTheme.typography.labelSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.testTag("approval-decision-identity-request-id")
                )
            }
            RiskBadge(
                risk = approvalRisk,
                readable = true,
                modifier = Modifier.testTag("approval-decision-identity-risk")
            )
        }
        if (!compactLayout) {
            Text(
                text = "Decision Gate",
                color = palette.ink,
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
        }
        Text(
            text = ApprovalDecisionPolicy.expiryCountdownLabel(expiresAt, now),
            color = if (ApprovalDecisionPolicy.expiryIsUrgent(expiresAt, now)) palette.caution else palette.inkMuted,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier
                .fillMaxWidth()
                .clearAndSetSemantics {
                    liveRegion = LiveRegionMode.Polite
                    contentDescription = ApprovalDecisionPolicy.expiryAccessibilityAnnouncement(expiresAt, now)
                }
                .testTag("approval-decision-expiry-countdown")
        )
        Spacer(modifier = Modifier.height(TowerSpacing.xs))
        Text(
            text = when {
                compactLayout && decisionNeedsReconciliation -> "Check server status."
                compactLayout && decisionInFlight -> "Recording decision…"
                compactLayout && hasUnsupportedData -> "This request contains an unrecognized status or risk value. Decisions are disabled."
                compactLayout && isBlocked -> "This request is blocked by policy. No decision can be recorded from mobile."
                compactLayout && decisionBlockReason != null -> decisionBlockReason
                compactLayout && exactToolRequestNeedsReview -> "Review request first."
                compactLayout && fullDiffReviewNeedsReview -> "Review full diff first."
                compactLayout && canApprove -> "Ready for decision."
                hasUnsupportedData -> "This request contains an unrecognized status or risk value. Decisions are disabled."
                decisionNeedsReconciliation -> "The previous decision may have reached the server, but its response was lost. Check server status before trying again."
                decisionInFlight -> "Sending the decision and waiting for the server."
                isBlocked -> "This request is blocked by policy. No decision can be recorded from mobile."
                !canDecide -> "This request is no longer waiting for a decision."
                fullDiffReviewNeedsReview && !fullDiffOpened -> "Open the full file diff, then acknowledge that you reviewed it before approving."
                fullDiffReviewNeedsReview -> "Acknowledge that you reviewed the full file diff before approving."
                exactToolRequestNeedsReview -> "Review the exact tool name and arguments above, then acknowledge before approving."
                canApprove -> "Approval is available. Confirm only after reviewing diff, tests, and rollback."
                else -> "Approval is disabled until safety requirements are satisfied."
            },
            modifier = Modifier
                .testTag("approval-decision-status")
                .semantics { liveRegion = LiveRegionMode.Polite },
            color = if (canApprove) palette.inkMuted else palette.caution,
            style = if (compactLayout) MaterialTheme.typography.bodySmall else MaterialTheme.typography.bodyLarge,
            maxLines = Int.MAX_VALUE,
            overflow = TextOverflow.Clip
        )
        Spacer(modifier = Modifier.height(if (compactLayout) TowerSpacing.xs else TowerSpacing.lg))
        if (decisionNeedsReconciliation) {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            OutlinedButton(
                onClick = onReconcileDecision,
                enabled = !decisionInFlight,
                modifier = Modifier.fillMaxWidth()
            ) { Text("Check server status") }
        }
        if (canDecide || showDisabledActions) {
            Spacer(modifier = Modifier.height(if (compactLayout) TowerSpacing.xs else TowerSpacing.md))
            Column(
                verticalArrangement = Arrangement.spacedBy(
                    if (compactLayout) TowerSpacing.xs else TowerSpacing.sm
                ),
                modifier = Modifier.fillMaxWidth()
            ) {
                OutlinedButton(
                    onClick = onReject,
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 56.dp)
                        .semantics(mergeDescendants = true) {
                            contentDescription = "Deny request ${shortSafeApprovalId(approvalId)} from ${approvalProviderName(approvalProvider)}: $approvalTitle. Risk: ${approvalRisk.name.lowercase().replace('_', ' ')}."
                        },
                    enabled = canDecide
                ) { Text("Deny", color = palette.danger) }
                Button(
                    onClick = onApprove,
                    enabled = canApprove,
                    colors = ButtonDefaults.buttonColors(containerColor = palette.brand, contentColor = palette.onBrand),
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 56.dp)
                        .semantics(mergeDescendants = true) {
                            contentDescription = "Approve request ${shortSafeApprovalId(approvalId)} from ${approvalProviderName(approvalProvider)}: $approvalTitle. Risk: ${approvalRisk.name.lowercase().replace('_', ' ')}."
                        }
                ) { Text("Approve request") }
            }
        }
    }
}
