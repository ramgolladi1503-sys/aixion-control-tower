package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.delay
import java.time.Duration
import java.time.Instant
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.components.DiffBlock
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.SourceBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
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
                reason = "Rejected from mobile detail review.",
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
    onReconcileDecision: () -> Unit = {}
) {
    val palette = LocalTowerPalette.current
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
    val canApprove = canDecide && !hasRequiredActions
    val requiresRiskConfirmation = requiresExplicitRiskConfirmation(approval.risk)
    var showRiskConfirmation by remember(approval.id) { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(palette.canvas)
    ) {
        LazyColumn(
            modifier = Modifier
                .testTag("approval-detail-list")
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
        ) {
            item {
                ApprovalHeroPanel(
                    approval = approval,
                    canApprove = canApprove,
                    canDecide = canDecide,
                    isBlocked = isBlocked,
                    hasUnsupportedData = hasUnsupportedData,
                    hasRequiredActions = hasRequiredActions
                )
            }

            if (lastActionMessage != null && lastActionApprovalId == approval.id) {
                item { DetailPanel(title = "Last Action", body = lastActionMessage) }
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
                item {
                    WarningPanel(
                        title = "Decision closed",
                        body = "This approval is ${approval.status.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() }}. Decisions are available only while it is pending."
                    )
                }
            }

            if (hasRequiredActions) {
                item { RequiredActionsPanel(actions = approval.requiredActions) }
            }

            item {
                TowerSectionHeader(
                    title = "Safety Context",
                    subtitle = "Review source, branch, test plan, and rollback before making a decision."
                )
            }

            item {
                DetailPanel(
                    title = "Request Identity",
                    body = buildList {
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
                    }.joinToString("\n")
                )
            }
            item {
                DetailPanel(
                    title = "Risk Rationale",
                    body = approval.riskReasons.ifEmpty {
                        listOf("No risk rationale was supplied by the backend.")
                    }.joinToString("\n")
                )
            }
            item { DetailPanel(title = "Test Plan", body = approval.testPlan.ifEmpty { listOf("No tests listed") }.joinToString("\n")) }
            item { DetailPanel(title = "Rollback Plan", body = approval.rollbackPlan.ifBlank { "No rollback plan provided" }) }

            item {
                Button(
                    onClick = onOpenDiff,
                    colors = ButtonDefaults.buttonColors(containerColor = palette.brand, contentColor = palette.canvas),
                    modifier = Modifier.fillMaxWidth()
                ) { Text("Review Full Diff") }
            }

            item {
                TowerSectionHeader(
                    title = "File Changes",
                    subtitle = "Inline preview only. Open the full diff before approving risky changes."
                )
            }

            items(approval.files) { file -> DiffBlock(file = file) }

        }

        if (canDecide || decisionInFlight || decisionNeedsReconciliation) {
            DecisionPanel(
                canApprove = canApprove,
                canDecide = canDecide,
                isBlocked = isBlocked,
                hasUnsupportedData = hasUnsupportedData,
                decisionInFlight = decisionInFlight,
                decisionNeedsReconciliation = decisionNeedsReconciliation,
                onReconcileDecision = onReconcileDecision,
                onApprove = {
                    if (requiresRiskConfirmation) {
                        showRiskConfirmation = true
                    } else {
                        onApprove()
                    }
                },
                onReject = onReject,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.sm)
            )
        }
    }

    if (showRiskConfirmation) {
        AlertDialog(
            onDismissRequest = { showRiskConfirmation = false },
            title = { Text("Confirm high-risk approval") },
            text = {
                Text(
                    "Action: ${approval.title}\n${approval.summary}\n\n" +
                        "Target: ${approval.projectName} · ${approval.targetBranch.ifBlank { "Not provided" }}\n" +
                        "Risk: ${approval.risk.name.lowercase().replace('_', ' ').replaceFirstChar { it.titlecase() }}"
                )
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

internal fun requiresExplicitRiskConfirmation(risk: RiskLevel): Boolean =
    risk == RiskLevel.HIGH || risk == RiskLevel.CRITICAL

@Composable
@OptIn(ExperimentalLayoutApi::class)
private fun ApprovalHeroPanel(
    approval: ApprovalSummary,
    canApprove: Boolean,
    canDecide: Boolean,
    isBlocked: Boolean,
    hasUnsupportedData: Boolean,
    hasRequiredActions: Boolean
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
                RiskBadge(approval.risk, readable = true)
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
            Text(text = approval.summary, color = palette.inkMuted, fontSize = 14.sp, lineHeight = 20.sp)
        }

        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        DecisionContextLine(
            "Risk rationale",
            approval.riskReasons.ifEmpty { listOf("No risk rationale was supplied.") }.joinToString("\n")
        )
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(palette.canvas.copy(alpha = 0.42f), RoundedCornerShape(16.dp))
                .padding(TowerSpacing.md),
            verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm)
        ) {
            Text("Decision context", color = palette.ink, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
            DecisionContextLine("Project", approval.projectName.ifBlank { "Not provided" })
            DecisionContextLine("Target", approval.targetBranch.ifBlank { "Not provided" })
            DecisionContextLine("Provider", "${approval.sourceLabel} · ${if (approval.verifiedSource) "Verified source" else "Source not verified"}")
            DecisionContextLine("Invocation", approval.sourceInvocationId?.ifBlank { null } ?: "Not provided")
            DecisionContextLine("Expires", approval.expiresAt?.ifBlank { null } ?: "Not provided")
        }
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
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Medium,
                    lineHeight = 20.sp
                )
                Text(
                    value,
                    color = palette.ink,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
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
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Medium,
                    lineHeight = 20.sp,
                    modifier = Modifier.width(96.dp)
                )
                Text(
                    value,
                    color = palette.ink,
                    fontSize = 14.sp,
                    lineHeight = 20.sp,
                    modifier = Modifier.weight(1f)
                )
            }
        }
    }
}

internal fun shouldStackDecisionContext(maxWidth: androidx.compose.ui.unit.Dp, fontScale: Float): Boolean =
    maxWidth < 360.dp || fontScale >= 1.5f

@Composable
private fun DetailPanel(title: String, body: String, modifier: Modifier = Modifier) {
    val palette = LocalTowerPalette.current
    TowerPanel(modifier = modifier, elevated = true) {
        Text(
                title,
            color = palette.inkMuted,
            fontSize = 14.sp,
            fontWeight = FontWeight.Medium,
            letterSpacing = 1.2.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = palette.ink, fontSize = 14.sp, lineHeight = 20.sp)
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
        Text(title, color = palette.danger, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = palette.ink, fontSize = 14.sp, lineHeight = 20.sp)
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
        Text("Required Before Approval", color = palette.danger, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        actions.forEach { action -> Text("• $action", color = palette.ink, fontSize = 14.sp, lineHeight = 21.sp) }
    }
}

@Composable
private fun DecisionPanel(
    canApprove: Boolean,
    canDecide: Boolean,
    isBlocked: Boolean,
    hasUnsupportedData: Boolean,
    decisionInFlight: Boolean,
    decisionNeedsReconciliation: Boolean,
    onReconcileDecision: () -> Unit,
    onApprove: () -> Unit,
    onReject: () -> Unit,
    modifier: Modifier = Modifier
) {
    val palette = LocalTowerPalette.current
    TowerPanel(modifier = modifier, elevated = true) {
        Text(
            text = "Decision Gate",
            color = palette.ink,
            fontSize = 18.sp,
            fontWeight = FontWeight.SemiBold
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = when {
                hasUnsupportedData -> "This request contains an unrecognized status or risk value. Decisions are disabled."
                decisionNeedsReconciliation -> "The previous decision may have reached the server, but its response was lost. Check server status before trying again."
                decisionInFlight -> "Sending the decision and waiting for the server."
                isBlocked -> "This request is blocked by policy. No decision can be recorded from mobile."
                !canDecide -> "This request is no longer waiting for a decision."
                canApprove -> "Approval is available. Confirm only after reviewing diff, tests, and rollback."
                else -> "Approval is disabled until safety requirements are satisfied."
            },
            modifier = Modifier
                .testTag("approval-decision-status")
                .semantics { liveRegion = LiveRegionMode.Polite },
            color = if (canApprove) palette.inkMuted else palette.caution,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        if (decisionNeedsReconciliation) {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            OutlinedButton(
                onClick = onReconcileDecision,
                enabled = !decisionInFlight,
                modifier = Modifier.fillMaxWidth()
            ) { Text("Check server status") }
        }
        if (canDecide) {
            Spacer(modifier = Modifier.height(TowerSpacing.md))
            Column(
                verticalArrangement = Arrangement.spacedBy(TowerSpacing.sm),
                modifier = Modifier.fillMaxWidth()
            ) {
                OutlinedButton(
                    onClick = onReject,
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp),
                    enabled = canDecide
                ) { Text("Reject", color = palette.danger) }
                Button(
                    onClick = onApprove,
                    enabled = canApprove,
                    colors = ButtonDefaults.buttonColors(containerColor = palette.positive, contentColor = palette.canvas),
                    modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp)
                ) { Text("Approve request") }
            }
        }
    }
}
