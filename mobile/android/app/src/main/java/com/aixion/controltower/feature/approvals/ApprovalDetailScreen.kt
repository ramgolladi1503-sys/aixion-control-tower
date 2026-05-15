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
import androidx.compose.material3.OutlinedButtonDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
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
import com.aixion.controltower.core.ui.theme.TowerBorderSoft
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerSurfaceRaised
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun ApprovalDetailScreen(
    viewModel: ApprovalsViewModel = viewModel(),
    onOpenDiff: () -> Unit = {},
    onDecisionSubmitted: () -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    val approval = state.selectedApproval ?: state.approvals.firstOrNull()

    if (approval == null) {
        EmptyApprovalDetail()
        return
    }

    ApprovalDetailContent(
        approval = approval,
        lastActionMessage = state.lastActionMessage,
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
private fun EmptyApprovalDetail() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.Center
    ) {
        TowerPanel(elevated = true) {
            Text("No approval selected", color = TowerTextPrimary, fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
            Spacer(modifier = Modifier.height(TowerSpacing.sm))
            Text("Open the inbox and select a request to review.", color = TowerTextMuted, fontSize = 14.sp)
        }
    }
}

@Composable
private fun ApprovalDetailContent(
    approval: ApprovalSummary,
    lastActionMessage: String?,
    onOpenDiff: () -> Unit,
    onApprove: () -> Unit,
    onReject: () -> Unit,
    onRevise: () -> Unit
) {
    val isBlocked = approval.status == ApprovalStatus.BLOCKED || approval.risk.name == "BLOCKED"
    val hasRequiredActions = approval.requiredActions.isNotEmpty()
    val canApprove = !isBlocked && !hasRequiredActions

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            ApprovalHeroPanel(
                approval = approval,
                canApprove = canApprove,
                isBlocked = isBlocked,
                hasRequiredActions = hasRequiredActions
            )
        }

        if (lastActionMessage != null) {
            item { DetailPanel(title = "Last Action", body = lastActionMessage) }
        }

        if (isBlocked) {
            item {
                WarningPanel(
                    title = "Approval blocked",
                    body = "This request violates a hard safety rule. It cannot be approved from mobile."
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
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.fillMaxWidth()) {
                DetailPanel(title = "Agent", body = approval.agentName, modifier = Modifier.weight(1f))
                DetailPanel(title = "Source", body = approval.sourceLabel, modifier = Modifier.weight(1f))
            }
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
                isBlocked = isBlocked,
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
    isBlocked: Boolean,
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
                        isBlocked -> "BLOCKED"
                        canApprove -> "READY FOR DECISION"
                        hasRequiredActions -> "SAFETY GATE"
                        else -> "REVIEW REQUIRED"
                    },
                    color = when {
                        isBlocked -> RiskBlocked
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
            fontSize = 10.sp,
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
    isBlocked: Boolean,
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
            text = if (canApprove) {
                "Approval is available. Confirm only after reviewing diff, tests, and rollback."
            } else {
                "Approval is disabled until safety requirements are satisfied."
            },
            color = if (canApprove) TowerTextMuted else RiskMedium,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        Button(
            onClick = onApprove,
            enabled = canApprove,
            colors = ButtonDefaults.buttonColors(containerColor = RiskLow, contentColor = TowerBackground),
            modifier = Modifier.fillMaxWidth()
        ) { Text("Approve") }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(
                onClick = onReject,
                modifier = Modifier.weight(1f),
                enabled = !isBlocked,
                colors = OutlinedButtonDefaults.outlinedButtonColors(contentColor = RiskCritical)
            ) { Text("Reject") }
            OutlinedButton(
                onClick = onRevise,
                modifier = Modifier.weight(1f),
                enabled = !isBlocked,
                colors = OutlinedButtonDefaults.outlinedButtonColors(contentColor = TowerTextPrimary)
            ) { Text("Revise") }
        }
    }
}
