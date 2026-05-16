package com.aixion.controltower.feature.workorders

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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.WorkOrderSummary
import com.aixion.controltower.core.ui.components.RiskBadge
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.components.TowerPanel
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSpacing
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerTextMuted
import com.aixion.controltower.core.ui.theme.TowerTextPrimary

@Composable
fun WorkOrdersScreen(viewModel: WorkOrdersViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Text("Work Orders", color = TowerTextPrimary, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(
                text = when {
                    state.loading -> "Loading work orders..."
                    state.hasError -> "Backend work-order sync failed. No mock work orders are shown."
                    else -> "Structured work plans created from Command, connected agents, or backend intake before approval or execution."
                },
                color = TowerTextMuted,
                fontSize = 14.sp,
                lineHeight = 20.sp
            )
        }

        item { WorkOrderMeaningPanel() }

        item { WorkOrderLifecyclePanel() }

        state.errorMessage?.let { message ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge("REAL DATA REQUIRED", RiskCritical)
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "Work-order data unavailable",
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
                    Button(onClick = viewModel::refresh, enabled = !state.loading) {
                        Text("Retry work orders")
                    }
                }
            }
        }

        if (state.hasError) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No fallback work orders loaded",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Authenticated work-order screens require real backend data. A fake Work Order would make users think something was saved or approved when it was not.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        } else if (!state.loading && state.workOrders.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No Work Orders yet",
                        color = TowerTextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Create one manually from Command, or connect ChatGPT/Codex through Connectors so external-agent work can enter the controlled workflow.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        } else {
            items(state.workOrders) { workOrder ->
                WorkOrderCard(workOrder = workOrder)
            }
        }
    }
}

@Composable
private fun WorkOrderMeaningPanel() {
    TowerPanel(elevated = true) {
        Text("What is a Work Order?", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "A Work Order is the prepared work package: goal, scope, source, risk, tasks, required tests, and rollback expectation.",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
        Text(
            text = "It is not the approval itself and it does not execute code. Sensitive work still needs an Approval before GitHub execution or agent continuation.",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}

@Composable
private fun WorkOrderLifecyclePanel() {
    TowerPanel(elevated = true) {
        Text("Lifecycle", color = TowerTextPrimary, fontSize = 16.sp, fontWeight = FontWeight.SemiBold)
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text("1. Created from Command, connected-agent intake, MCP/backend flow, or operator action.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("2. Reviewed as a structured plan with source, risk, tasks, and tests.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("3. Routed to Approvals when a decision is required.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        Text("4. Approved work may continue to Agent Work, validation, GitHub execution, PR, or evidence.", color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
    }
}

@Composable
private fun WorkOrderCard(workOrder: WorkOrderSummary) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            RiskBadge(workOrder.risk)
            StatusBadge(workOrder.projectName, TowerAccent)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(workOrder.sourceBadgeLabel, if (workOrder.verifiedSource) RiskLow else RiskMedium)
            StatusBadge(workOrder.sourceLabel, TowerAccent)
        }
        Text("Source", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
        Text(sourceDescription(workOrder), color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 19.sp)
        optionalSourceReference(workOrder)?.let { reference ->
            Text(reference, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        }
        Text("Lifecycle state", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
        Text("Prepared package — not approved and not executed from this screen.", color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 19.sp)
        Text("Next action", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
        Text(nextAction(workOrder), color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 19.sp)
        Text(workOrder.goal, color = TowerTextPrimary, fontSize = 17.sp, fontWeight = FontWeight.Bold)
        Text("Tasks", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
        workOrder.tasks.ifEmpty { listOf("No tasks defined yet") }.forEach { task ->
            Text("• $task", color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 18.sp)
        }
        Text("Required Tests", color = TowerTextMuted, fontSize = 12.sp, fontWeight = FontWeight.Medium)
        workOrder.requiredTests.ifEmpty { listOf("No tests defined yet") }.forEach { test ->
            Text("• $test", color = TowerTextPrimary, fontSize = 13.sp, lineHeight = 18.sp)
        }
    }
}

private fun sourceDescription(workOrder: WorkOrderSummary): String {
    val sourceType = workOrder.sourceType.ifBlank { "MANUAL" }
    val provider = workOrder.sourceProvider.ifBlank { sourceType }
    return when {
        workOrder.verifiedSource -> "Verified connected-agent source: $provider."
        provider.equals("MANUAL", ignoreCase = true) || sourceType.equals("MANUAL", ignoreCase = true) -> "Manual/operator-created work package."
        else -> "Unverified external or backend source: $provider. Confirm the source before trusting execution."
    }
}

private fun optionalSourceReference(workOrder: WorkOrderSummary): String? {
    val parts = listOfNotNull(
        workOrder.sourceTaskId?.takeIf { it.isNotBlank() }?.let { "Task $it" },
        workOrder.sourceSessionId?.takeIf { it.isNotBlank() }?.let { "Session $it" },
        workOrder.sourceAgentId?.takeIf { it.isNotBlank() }?.let { "Agent $it" },
        workOrder.createdByUserId?.takeIf { it.isNotBlank() }?.let { "User ${it.take(8)}" }
    )
    return parts.takeIf { it.isNotEmpty() }?.joinToString(" • ")
}

private fun nextAction(workOrder: WorkOrderSummary): String {
    return if (workOrder.verifiedSource || !workOrder.sourceProvider.equals("MANUAL", ignoreCase = true)) {
        "Review the package, then use Approvals when a decision is required before agent or GitHub execution continues."
    } else {
        "Review the package. If it becomes sensitive or executable, route it through Approvals before execution."
    }
}