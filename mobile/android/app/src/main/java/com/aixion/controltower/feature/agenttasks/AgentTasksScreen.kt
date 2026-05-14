package com.aixion.controltower.feature.agenttasks

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.model.AgentTaskEventSummary
import com.aixion.controltower.core.model.AgentTaskStatus
import com.aixion.controltower.core.model.AgentTaskSummary
import com.aixion.controltower.core.ui.components.StatusBadge
import com.aixion.controltower.core.ui.theme.RiskCritical
import com.aixion.controltower.core.ui.theme.RiskHigh
import com.aixion.controltower.core.ui.theme.RiskLow
import com.aixion.controltower.core.ui.theme.RiskMedium
import com.aixion.controltower.core.ui.theme.TowerAccent
import com.aixion.controltower.core.ui.theme.TowerBackground
import com.aixion.controltower.core.ui.theme.TowerSurface
import com.aixion.controltower.core.ui.theme.TowerSurfaceRaised
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
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.weight(1f)) {
                    Text(
                        text = "Agent Tasks",
                        color = TowerTextPrimary,
                        fontSize = 28.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = if (state.loading) {
                            "Loading connected-agent work..."
                        } else {
                            "Track ChatGPT, Codex, Claude, Cursor, GitHub Actions, and manual agent work."
                        },
                        color = TowerTextMuted,
                        fontSize = 14.sp
                    )
                }
                Button(onClick = { viewModel.refresh() }) {
                    Text("Refresh")
                }
            }
            state.errorMessage?.let { error ->
                Text(
                    text = "Backend error: $error",
                    color = RiskCritical,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.padding(top = 8.dp)
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

        if (!state.loading && state.tasks.isEmpty()) {
            item {
                Text(
                    text = if (state.errorMessage == null) {
                        "No agent tasks found yet. Create one from GPT Actions, Codex, or the backend task API."
                    } else {
                        "Agent task list unavailable. Fix backend/auth connectivity before trusting this screen."
                    },
                    color = TowerTextMuted,
                    fontSize = 15.sp,
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(TowerSurface, RoundedCornerShape(22.dp))
                        .padding(18.dp)
                )
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
private fun AgentTaskSummaryCard(total: Int, active: Int, waiting: Int) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(24.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "TOTAL $total", color = TowerAccent)
            StatusBadge(label = "ACTIVE $active", color = RiskMedium)
            StatusBadge(label = "WAITING $waiting", color = if (waiting > 0) RiskHigh else RiskLow)
        }
        Text(
            text = "Connected-agent work should be visible here before it becomes approvals, PRs, failures, or final evidence.",
            color = TowerTextMuted,
            fontSize = 13.sp
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
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(if (selected) TowerSurfaceRaised else TowerSurface, RoundedCornerShape(22.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
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
        Text(task.title, color = TowerTextPrimary, fontSize = 18.sp, fontWeight = FontWeight.Bold)
        Text(task.goal, color = TowerTextMuted, fontSize = 13.sp)
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
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurfaceRaised, RoundedCornerShape(24.dp))
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp)
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "TIMELINE", color = TowerAccent)
            StatusBadge(label = task.provider, color = task.statusColor())
        }
        Text(task.title, color = TowerTextPrimary, fontSize = 20.sp, fontWeight = FontWeight.Bold)
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
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(TowerSurface, RoundedCornerShape(16.dp))
            .padding(12.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = event.eventType, color = TowerAccent)
            event.status?.let { status -> StatusBadge(label = status.name, color = status.statusColor()) }
        }
        Text(event.message.ifBlank { "No message" }, color = TowerTextPrimary, fontSize = 14.sp)
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
