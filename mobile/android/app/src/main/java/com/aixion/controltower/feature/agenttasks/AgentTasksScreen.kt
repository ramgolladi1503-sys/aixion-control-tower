package com.aixion.controltower.feature.agenttasks

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
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.AgentTaskEventSummary
import com.aixion.controltower.core.model.AgentTaskStatus
import com.aixion.controltower.core.model.AgentTaskSummary
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
fun AgentTasksScreen(
    viewModel: AgentTasksViewModel = viewModel(),
    deepLinkTaskId: String? = null,
    onOpenApproval: (String) -> Unit = {}
) {
    val state by viewModel.state.collectAsState()
    val selectedTask = state.selectedTask

    LaunchedEffect(deepLinkTaskId) {
        deepLinkTaskId?.takeIf { it.isNotBlank() }?.let { taskId ->
            viewModel.openFromDeepLink(taskId)
        }
    }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            AgentTasksHero(
                loading = state.loading,
                total = state.tasks.size,
                active = state.activeCount,
                waiting = state.waitingForApprovalCount,
                onRefresh = { viewModel.refresh() }
            )
            state.errorMessage?.let { error ->
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Backend error: $error",
                    color = RiskCritical,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold
                )
            }
        }

        item {
            AgentTaskSummaryCard(
                total = state.tasks.size,
                active = state.activeCount,
                waiting = state.waitingForApprovalCount
            )
        }

        selectedTask?.let { task ->
            item {
                SelectedTaskTimelineCard(
                    task = task,
                    events = state.selectedEvents,
                    onBack = { viewModel.clearSelection() },
                    onOpenApproval = {
                        task.approvalRequestId?.let(onOpenApproval)
                    }
                )
            }
        }

        item {
            TowerSectionHeader(
                title = "Connected-Agent Work Queue",
                subtitle = "Tasks submitted by ChatGPT, Codex, Claude, Cursor, local bridges, or other connectors before they become approvals, execution, PRs, or failure evidence."
            )
        }

        if (!state.loading && state.tasks.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = if (state.errorMessage == null) {
                            "No connected-agent work yet. Create a connector, send a task through ChatGPT/Codex, or use the backend AgentTask API."
                        } else {
                            "Agent task list unavailable. Fix backend/auth connectivity before trusting this screen."
                        },
                        color = TowerTextMuted,
                        fontSize = 15.sp,
                        lineHeight = 21.sp
                    )
                    Spacer(modifier = Modifier.height(TowerSpacing.sm))
                    Text(
                        text = "A connector is the doorway. An Agent Task is the submitted work item. An Approval is the decision gate.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 19.sp
                    )
                }
            }
        }

        items(state.tasks) { task ->
            AgentTaskCard(
                task = task,
                selected = task.id == state.selectedTaskId,
                onSelect = { viewModel.selectTask(task.id) },
                onOpenApproval = {
                    task.approvalRequestId?.let(onOpenApproval)
                }
            )
        }
    }
}

@Composable
private fun AgentTasksHero(
    loading: Boolean,
    total: Int,
    active: Int,
    waiting: Int,
    onRefresh: () -> Unit
) {
    TowerHeroPanel {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Top
        ) {
            Column(modifier = Modifier.weight(1f)) {
                StatusBadge(label = if (loading) "SYNCING" else "CONNECTED AGENTS", color = if (loading) RiskMedium else RiskLow)
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Agent Tasks",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold,
                    lineHeight = 32.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.sm))
                Text(
                    text = if (loading) {
                        "Loading connected-agent work..."
                    } else {
                        "See work submitted by external agents. This is not the connector setup screen and not the final approval screen."
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
            StatusBadge(label = "TOTAL $total", color = TowerAccent)
            StatusBadge(label = "ACTIVE $active", color = RiskMedium)
            StatusBadge(label = "WAITING $waiting", color = if (waiting > 0) RiskHigh else RiskLow)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Button(onClick = onRefresh, modifier = Modifier.fillMaxWidth()) {
            Text("Refresh")
        }
    }
}

@Composable
private fun AgentTaskSummaryCard(total: Int, active: Int, waiting: Int) {
    TowerPanel(elevated = true) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "TOTAL $total", color = TowerAccent)
            StatusBadge(label = "ACTIVE $active", color = RiskMedium)
            StatusBadge(label = "WAITING $waiting", color = if (waiting > 0) RiskHigh else RiskLow)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            text = "Agent Tasks are submitted work items. They can become approval requests, execution runs, pull-request evidence, failures, or completed records.",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
        Text(
            text = "If five or more agents are connected, this screen should scale as a queue grouped by source, status, and linked approval. That grouping is a follow-up UX scope.",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}

@Composable
private fun AgentTaskCard(
    task: AgentTaskSummary,
    selected: Boolean,
    onSelect: () -> Unit,
    onOpenApproval: () -> Unit
) {
    TowerPanel(elevated = selected) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = task.status.name, color = task.statusColor())
            StatusBadge(label = task.sourceLabel, color = TowerAccent)
            task.riskHint?.let { risk ->
                StatusBadge(label = risk.name, color = risk.color())
            }
            if (task.requiresApproval) {
                StatusBadge(label = "APPROVAL", color = RiskHigh)
            }
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(task.title, color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.SemiBold, lineHeight = 23.sp)
        Text(task.goal, color = TowerTextMuted, fontSize = 13.sp, lineHeight = 19.sp)
        task.repository?.takeIf { it.isNotBlank() }?.let { repository ->
            Text("Repo: $repository", color = TowerTextMuted, fontSize = 12.sp)
        }
        task.branchPreference?.takeIf { it.isNotBlank() }?.let { branch ->
            Text("Branch: $branch", color = TowerTextMuted, fontSize = 12.sp)
        }
        Text(
            text = "Action: ${task.requestedAction} • Updated: ${task.updatedAt ?: "unknown"}",
            color = TowerTextMuted,
            fontSize = 12.sp
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        OutlinedButton(onClick = onSelect, modifier = Modifier.fillMaxWidth()) {
            Text(if (selected) "Timeline loaded" else "View timeline")
        }
        if (task.hasLinkedApproval) {
            Button(onClick = onOpenApproval, modifier = Modifier.fillMaxWidth()) {
                Text("Open linked approval")
            }
        }
    }
}

@Composable
private fun SelectedTaskTimelineCard(
    task: AgentTaskSummary,
    events: List<AgentTaskEventSummary>,
    onBack: () -> Unit,
    onOpenApproval: () -> Unit
) {
    TowerHeroPanel {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "TIMELINE", color = TowerAccent)
            StatusBadge(label = task.provider, color = task.statusColor())
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(task.title, color = TowerTextPrimary, fontSize = 20.sp, fontWeight = FontWeight.SemiBold, lineHeight = 25.sp)
        if (task.approvalRequestId != null) {
            Button(onClick = onOpenApproval, modifier = Modifier.fillMaxWidth()) {
                Text("Open approval ${task.approvalRequestId}")
            }
        }
        if (events.isEmpty()) {
            Text("No timeline events loaded yet.", color = TowerTextMuted, fontSize = 13.sp)
        } else {
            events.forEach { event -> AgentTaskEventRow(event) }
        }
        OutlinedButton(onClick = onBack, modifier = Modifier.fillMaxWidth()) {
            Text("Close timeline")
        }
    }
}

@Composable
private fun AgentTaskEventRow(event: AgentTaskEventSummary) {
    TowerPanel(elevated = false) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = event.eventType, color = TowerAccent)
            event.status?.let { status -> StatusBadge(label = status.name, color = status.statusColor()) }
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(event.message.ifBlank { "No message" }, color = TowerTextPrimary, fontSize = 14.sp, lineHeight = 20.sp)
        Text(
            text = "${event.actor} • ${event.createdAt ?: "recent"}",
            color = TowerTextMuted,
            fontSize = 12.sp
        )
    }
}

private fun AgentTaskSummary.statusColor(): Color = status.statusColor()

private fun AgentTaskStatus.statusColor(): Color {
    return when (this) {
        AgentTaskStatus.RECEIVED,
        AgentTaskStatus.PLANNING -> TowerAccent
        AgentTaskStatus.WAITING_FOR_APPROVAL -> RiskHigh
        AgentTaskStatus.APPROVED,
        AgentTaskStatus.EXECUTING,
        AgentTaskStatus.TESTING -> RiskMedium
        AgentTaskStatus.READY_FOR_PR,
        AgentTaskStatus.DONE -> RiskLow
        AgentTaskStatus.DENIED,
        AgentTaskStatus.FAILED,
        AgentTaskStatus.CANCELLED -> RiskCritical
    }
}

private fun com.aixion.controltower.core.model.RiskLevel.color(): Color {
    return when (this) {
        com.aixion.controltower.core.model.RiskLevel.LOW -> RiskLow
        com.aixion.controltower.core.model.RiskLevel.MEDIUM -> RiskMedium
        com.aixion.controltower.core.model.RiskLevel.HIGH -> RiskHigh
        com.aixion.controltower.core.model.RiskLevel.CRITICAL,
        com.aixion.controltower.core.model.RiskLevel.BLOCKED -> RiskCritical
    }
}