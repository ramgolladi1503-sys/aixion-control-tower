package com.aixion.controltower.feature.mcp

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
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.MCPPendingHealthSummary
import com.aixion.controltower.core.model.MCPPendingStatus
import com.aixion.controltower.core.model.MCPPendingSummary
import com.aixion.controltower.core.ui.components.ForgedLogoMark
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerHeroPanel
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.components.TowerSectionHeader
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskHigh
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun MCPQueueScreen(
    viewModel: MCPQueueViewModel = viewModel(),
    onOpenApproval: (String) -> Unit = {}
) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            MCPQueueHero(
                loading = state.loading,
                health = state.health,
                pendingCount = state.pendingRequests.size
            )
            state.message?.let { message ->
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(text = message, color = TowerAccent, fontSize = 13.sp)
            }
            state.errorMessage?.let { error ->
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = "Backend error: $error",
                    color = RiskCritical,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold
                )
            }
        }

        state.health?.let { health ->
            item { MCPHealthCard(health) }
        }

        item {
            TowerSectionHeader(
                title = "Pending MCP Work",
                subtitle = "Mutating child-server tool calls stay visible here until approval, forwarding, or recovery is complete."
            )
        }

        if (!state.loading && state.pendingRequests.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = if (state.errorMessage == null) {
                            "No MCP pending work found."
                        } else {
                            "MCP queue unavailable. Fix backend connectivity instead of trusting mock queue data."
                        },
                        color = TowerTextMuted,
                        fontSize = 15.sp,
                        lineHeight = 21.sp
                    )
                }
            }
        }

        items(state.pendingRequests) { pending ->
            MCPPendingCard(
                pending = pending,
                recovering = state.recoveringPendingId == pending.id,
                onOpenApproval = { onOpenApproval(pending.approvalRequestId) },
                onRecover = { viewModel.recoverPendingRequest(pending.id) }
            )
        }
    }
}

@Composable
private fun MCPQueueHero(
    loading: Boolean,
    health: MCPPendingHealthSummary?,
    pendingCount: Int
) {
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(
                    label = if (loading) "SYNCING" else "MCP WAIT MODE",
                    color = if (health?.isHealthy == false) RiskCritical else if (loading) RiskMedium else RiskLow
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "MCP Queue",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 32.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = if (loading) {
                        "Loading MCP queue state..."
                    } else {
                        "Watch blocked, forwarding, retryable, and dead-lettered MCP work."
                    },
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
            }
            ForgedLogoMark(size = 52.dp, color = TowerTextPrimary.copy(alpha = 0.78f))
        }
        Spacer(modifier = Modifier.height(TowerSpacing.lg))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "PENDING $pendingCount", color = TowerAccent)
            health?.let {
                StatusBadge(label = "ATTENTION ${it.attentionRequired}", color = if (it.attentionRequired > 0) RiskCritical else RiskLow)
            }
        }
    }
}

@Composable
private fun MCPHealthCard(health: MCPPendingHealthSummary) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(
                label = if (health.isHealthy) "HEALTHY" else "ATTENTION",
                color = if (health.isHealthy) RiskLow else RiskCritical
            )
            StatusBadge(label = "TOTAL ${health.total}", color = TowerAccent)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            MCPMetric("Waiting", health.waitingForApproval.toString(), TowerAccent, Modifier.weight(1f))
            MCPMetric("Forwarding", health.forwarding.toString(), RiskMedium, Modifier.weight(1f))
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), modifier = Modifier.fillMaxWidth()) {
            MCPMetric("Dead", health.deadLetter.toString(), RiskCritical, Modifier.weight(1f))
            MCPMetric("Retryable", health.retryable.toString(), RiskHigh, Modifier.weight(1f))
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "Attention required: ${health.attentionRequired} • Active leases: ${health.activeLeases} • Expired leases: ${health.expiredLeases}",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}

@Composable
private fun MCPMetric(label: String, value: String, color: Color, modifier: Modifier = Modifier) {
    TowerPanel(modifier = modifier, elevated = false) {
        Text(text = value, color = color, fontSize = 24.sp, fontWeight = FontWeight.SemiBold)
        Text(text = label, color = TowerTextMuted, fontSize = 12.sp)
    }
}

@Composable
private fun MCPPendingCard(
    pending: MCPPendingSummary,
    recovering: Boolean,
    onOpenApproval: () -> Unit,
    onRecover: () -> Unit
) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = pending.status.name, color = pending.statusColor())
            if (pending.needsOperatorAttention) {
                StatusBadge(label = "NEEDS ATTENTION", color = RiskCritical)
            } else if (pending.isRetryable) {
                StatusBadge(label = "RETRYABLE", color = RiskHigh)
            }
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "${pending.serverName}.${pending.toolName}",
            color = TowerTextPrimary,
            fontSize = 18.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 23.sp
        )
        Text(
            text = "Approval ${pending.approvalRequestId} • requested by ${pending.requestedBy}",
            color = TowerTextMuted,
            fontSize = 13.sp
        )
        Text(
            text = "Attempts ${pending.attempts}/${pending.maxAttempts}" +
                pending.leaseOwner?.let { " • Lease: $it" }.orEmpty(),
            color = TowerTextMuted,
            fontSize = 13.sp
        )
        pending.lastError?.takeIf { it.isNotBlank() }?.let { error ->
            Text(
                text = "Last error: $error",
                color = RiskCritical,
                fontSize = 13.sp,
                fontWeight = FontWeight.Bold
            )
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        OutlinedButton(onClick = onOpenApproval, modifier = Modifier.fillMaxWidth()) {
            Text("Open linked approval")
        }
        if (pending.canRecoverFromMobile()) {
            Button(
                onClick = onRecover,
                enabled = !recovering,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(if (recovering) "Recovering..." else "Recover MCP request")
            }
        }
    }
}

private fun MCPPendingSummary.canRecoverFromMobile(): Boolean {
    return status == MCPPendingStatus.DEAD_LETTER || needsOperatorAttention
}

private fun MCPPendingSummary.statusColor(): Color {
    return when (status) {
        MCPPendingStatus.WAITING_FOR_APPROVAL -> TowerAccent
        MCPPendingStatus.FORWARDING -> RiskMedium
        MCPPendingStatus.FORWARDED -> RiskLow
        MCPPendingStatus.BLOCKED_BY_DECISION -> RiskHigh
        MCPPendingStatus.ORPHANED -> RiskHigh
        MCPPendingStatus.DEAD_LETTER -> RiskCritical
    }
}