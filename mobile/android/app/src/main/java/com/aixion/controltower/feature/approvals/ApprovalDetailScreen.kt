package com.aixion.controltower.feature.approvals

import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.foundation.shape.RoundedCornerShape
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
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.SourceBadge
import com.aixion.controltower.core.ui.components.StatusBadge
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
        onRevise = {
            viewModel.decide(
                decision = "revise",
                reason = "Revision requested from mobile detail review.",
                onCompleted = onDecisionSubmitted
            )
        }
    )
}

@Composable
internal fun ApprovalDetailEmptyContent(
    loading: Boolean,
    message: String?,
    onRetry: (() -> Unit)?
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(horizontal = towerScreenHorizontalPadding(), vertical = TowerSpacing.lg),
        verticalArrangement = Arrangement.Center
    ) {
        TowerPanel(elevated = true) {
            when {
                loading -> {
                    Text(
                        "Loading requested approval…",
                        modifier = Modifier.testTag("approval-detail-loading"),
                        color = TowerTextPrimary,
                        fontSize = 18.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text("The app is fetching this approval from the authenticated backend.", color = TowerTextMuted, fontSize = 14.sp)
                }
                message != null -> {
                    Text("Approval unavailable", color = TowerTextPrimary, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(message, modifier = Modifier.testTag("approval-detail-open-error"), color = TowerTextMuted, fontSize = 14.sp)
                    onRetry?.let { retry ->
                        Spacer(modifier = Modifier.height(TowerSpacing.md))
                        OutlinedButton(onClick = retry, modifier = Modifier.fillMaxWidth()) {
                            Text("Retry approval load")
                        }
                    }
                }
                else -> {
                    Text("No approval selected", color = TowerTextPrimary, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text("Open the inbox and select a request to review.", color = TowerTextMuted, fontSize = 14.sp)
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
    onRevise: () -> Unit,
    lastActionApprovalId: String? = null,
    decisionInFlight: Boolean = false,
    decisionNeedsReconciliation: Boolean = false,
    onReconcileDecision: () -> Unit = {}
) {
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

    LazyColumn(
        modifier = Modifier
            .testTag("approval-detail-list")
            .fillMaxSize()
            .background(TowerBackground)
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
        } else if (!canDecide) {
            item {
                WarningPanel(
                    title = "Decision closed",
                    body = "This approval is ${approval.status.name}. Decisions are available only while it is pending."
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
                colors = ButtonDefaults.buttonColors(containerColor = TowerAccent, contentColor = TowerBackground),
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

        item {
            DecisionPanel(
                canApprove = canApprove,
                canDecide = canDecide,
                isBlocked = isBlocked,
                hasUnsupportedData = hasUnsupportedData,
                decisionInFlight = decisionInFlight,
                decisionNeedsReconciliation = decisionNeedsReconciliation,
                onReconcileDecision = onReconcileDecision,
                onApprove = onApprove,
                onReject = onReject,
                onRevise = onRevise
            )
        }
    }
}

@Composable
private fun ApprovalHeroPanel(
    approval: ApprovalSummary,
    canApprove: Boolean,
    canDecide: Boolean,
    isBlocked: Boolean,
    hasUnsupportedData: Boolean,
    hasRequiredActions: Boolean
) {
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(
                    label = when {
                        hasUnsupportedData -> "UNSUPPORTED DATA"
                        isBlocked -> "BLOCKED"
                        !canDecide -> "DECISION CLOSED"
                        canApprove -> "READY FOR DECISION"
                        hasRequiredActions -> "SAFETY GATE"
                        else -> "REVIEW REQUIRED"
                    },
                    color = when {
                        isBlocked || hasUnsupportedData -> RiskCritical
                        canApprove -> RiskLow
                        else -> RiskMedium
                    }
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = approval.title,
                    color = TowerTextPrimary,
                    fontSize = 25.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 30.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(text = approval.summary, color = TowerTextMuted, fontSize = 14.sp, lineHeight = 20.sp)
            }
            ForgedLogoMark(size = 50.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
        }

        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            RiskBadge(approval.risk)
            StatusBadge(approval.status.name, TowerTextMuted)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SourceBadge(approval)
            StatusBadge(approval.targetBranch, TowerAccent)
        }
    }
}

@Composable
private fun DetailPanel(title: String, body: String, modifier: Modifier = Modifier) {
    TowerPanel(modifier = modifier, elevated = true) {
        Text(
            title.uppercase(),
            color = TowerTextMuted,
            fontSize = 14.sp,
            fontWeight = FontWeight.Medium,
            letterSpacing = 1.2.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = TowerTextPrimary, fontSize = 14.sp, lineHeight = 20.sp)
    }
}

@Composable
private fun WarningPanel(title: String, body: String) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(RiskBlocked.copy(alpha = 0.12f), RoundedCornerShape(24.dp))
            .border(1.dp, RiskBlocked.copy(alpha = 0.32f), RoundedCornerShape(24.dp))
            .padding(16.dp)
    ) {
        Text(title, color = RiskBlocked, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(body, color = TowerTextPrimary, fontSize = 14.sp, lineHeight = 20.sp)
    }
}

@Composable
private fun RequiredActionsPanel(actions: List<String>) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(RiskCritical.copy(alpha = 0.10f), RoundedCornerShape(24.dp))
            .border(1.dp, RiskCritical.copy(alpha = 0.28f), RoundedCornerShape(24.dp))
            .padding(16.dp)
    ) {
        Text("Required Before Approval", color = RiskCritical, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        actions.forEach { action -> Text("• $action", color = TowerTextPrimary, fontSize = 14.sp, lineHeight = 21.sp) }
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
    onRevise: () -> Unit
) {
    TowerPanel(elevated = true) {
        Text(
            text = "Decision Gate",
            color = TowerTextPrimary,
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
            color = if (canApprove) TowerTextMuted else RiskMedium,
            fontSize = 14.sp,
            lineHeight = 21.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        Button(
            onClick = onApprove,
            enabled = canApprove,
            colors = ButtonDefaults.buttonColors(containerColor = RiskLow, contentColor = TowerBackground),
            modifier = Modifier.fillMaxWidth()
        ) { Text("Approve") }
        if (decisionNeedsReconciliation) {
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            OutlinedButton(
                onClick = onReconcileDecision,
                enabled = !decisionInFlight,
                modifier = Modifier.fillMaxWidth()
            ) { Text("Check server status") }
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(
                onClick = onReject,
                modifier = Modifier.weight(1f),
                enabled = canDecide
            ) { Text("Reject", color = RiskCritical) }
            OutlinedButton(
                onClick = onRevise,
                modifier = Modifier.weight(1f),
                enabled = canDecide
            ) { Text("Revise", color = TowerTextPrimary) }
        }
    }
}
