package com.aixion.controltower.feature.agentruns

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aixion.controltower.core.api.dto.AgentReliabilityScorecardDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunDto
import com.aixion.controltower.core.api.dto.AgentRunStepDto
import com.aixion.controltower.core.api.dto.AgentRunSummaryDto
import com.aixion.controltower.core.api.dto.TrustExceptionDto
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
import kotlin.math.roundToInt

@Composable
fun AgentRunsScreen(viewModel: AgentRunsViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TowerBackground)
            .padding(18.dp),
        verticalArrangement = Arrangement.spacedBy(TowerSpacing.lg)
    ) {
        item {
            TowerHeroPanel {
                StatusBadge(
                    label = if (state.loading) "SYNCING" else "MISSION CONTROL",
                    color = if (state.loading) RiskMedium else TowerAccent
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Text(
                    text = "Agent Trust Console",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold
                )
                Text(
                    text = "Review exceptions, supervise durable runs, and compare evidence-backed agent reliability without turning the phone into a terminal.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Button(onClick = viewModel::refresh, modifier = Modifier.fillMaxWidth()) {
                    Text("Refresh trust truth")
                }
            }
        }

        item { RunSummaryPanel(state.summary) }

        if (state.trustExceptions.isNotEmpty()) {
            item { TrustExceptionQueuePanel(state.trustExceptions) }
        }

        if (state.reliabilityScorecards.isNotEmpty()) {
            item { AgentReliabilityPanel(state.reliabilityScorecards) }
        }

        state.errorMessage?.let { error ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge(label = "BACKEND ERROR", color = RiskCritical)
                    Text(error, color = RiskCritical, fontSize = 13.sp, lineHeight = 19.sp)
                }
            }
        }

        state.actionMessage?.let { message ->
            item {
                TowerPanel(elevated = false) {
                    StatusBadge(label = "ACTION RECORDED", color = RiskLow)
                    Text(message, color = TowerTextPrimary, fontSize = 13.sp)
                }
            }
        }

        state.selected?.let { detail ->
            item {
                RunDetailPanel(
                    detail = detail,
                    actionInProgress = state.actionInProgress,
                    onPause = viewModel::pause,
                    onResume = viewModel::resume,
                    onCancel = viewModel::cancel,
                    onRetry = viewModel::retryCurrentStep,
                    onExecuteNext = viewModel::executeNext,
                    onClose = viewModel::closeRun
                )
            }
        }

        item {
            TowerSectionHeader(
                title = "Durable Run Queue",
                subtitle = "A run exists only after approval. Every step carries attempts, evidence, state transitions and a correlation ID."
            )
        }

        if (!state.loading && state.runs.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    Text(
                        text = "No durable Agent Runs exist yet. Approve eligible Agent Work to create a governed run.",
                        color = TowerTextMuted,
                        fontSize = 14.sp,
                        lineHeight = 20.sp
                    )
                }
            }
        }

        items(state.runs) { run ->
            RunCard(run = run, onOpen = { viewModel.openRun(run.id) })
        }
    }
}

@Composable
private fun RunSummaryPanel(summary: AgentRunSummaryDto) {
    TowerPanel(elevated = true) {
        Text(
            "Execution truth",
            color = TowerTextPrimary,
            fontSize = 17.sp,
            fontWeight = FontWeight.SemiBold
        )
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Row(horizontalArrangement = Arrangement.spacedBy(7.dp)) {
            StatusBadge(label = "ACTIVE ${summary.active}", color = TowerAccent)
            StatusBadge(
                label = "ACTION ${summary.needsHuman}",
                color = if (summary.needsHuman > 0) RiskHigh else RiskLow
            )
            StatusBadge(
                label = "FAILED ${summary.failed}",
                color = if (summary.failed > 0) RiskCritical else RiskLow
            )
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = "Queue ${summary.queueDepth} • Retry wait ${summary.retryWait} • Blocked ${summary.blocked} • Completed ${summary.succeeded}",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}

@Composable
private fun TrustExceptionQueuePanel(exceptions: List<TrustExceptionDto>) {
    TowerHeroPanel {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "NEEDS ATTENTION ${exceptions.size}", color = RiskHigh)
            val critical = exceptions.count { it.severity in setOf("CRITICAL", "BLOCKED") }
            StatusBadge(
                label = "CRITICAL $critical",
                color = if (critical > 0) RiskCritical else RiskLow
            )
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            text = "Exception queue",
            color = TowerTextPrimary,
            fontSize = 19.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            text = "Only agent actions and runs requiring judgment are surfaced here.",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 18.sp
        )
        exceptions.take(5).forEach { item ->
            TowerPanel(elevated = item.severity in setOf("CRITICAL", "BLOCKED")) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusBadge(label = item.category, color = exceptionColor(item.severity))
                    StatusBadge(label = item.severity, color = exceptionColor(item.severity))
                }
                Text(
                    text = item.title,
                    color = TowerTextPrimary,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.SemiBold
                )
                Text(
                    text = item.summary,
                    color = TowerTextMuted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
                item.runId?.let {
                    Text("Run $it", color = TowerTextMuted, fontSize = 11.sp)
                }
                item.leaseId?.let {
                    Text("Lease $it", color = TowerTextMuted, fontSize = 11.sp)
                }
            }
        }
        if (exceptions.size > 5) {
            Text(
                text = "+${exceptions.size - 5} more exceptions. Refresh after resolving the highest-risk items.",
                color = TowerTextMuted,
                fontSize = 12.sp
            )
        }
    }
}

@Composable
private fun AgentReliabilityPanel(scorecards: List<AgentReliabilityScorecardDto>) {
    TowerPanel(elevated = true) {
        Text(
            text = "Evidence-backed agent reliability",
            color = TowerTextPrimary,
            fontSize = 17.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            text = "Scores are computed from policy decisions, run attempts, recovery, evidence and intervention—not an LLM opinion.",
            color = TowerTextMuted,
            fontSize = 12.sp,
            lineHeight = 17.sp
        )
        scorecards.take(4).forEach { card ->
            TowerPanel(elevated = false) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusBadge(label = card.provider, color = TowerAccent)
                    StatusBadge(
                        label = "SCOPE ${(card.scopeAdherenceRate * 100).roundToInt()}%",
                        color = rateColor(card.scopeAdherenceRate)
                    )
                    StatusBadge(
                        label = "EVIDENCE ${(card.evidenceCompletionRate * 100).roundToInt()}%",
                        color = rateColor(card.evidenceCompletionRate)
                    )
                }
                Text(
                    text = "Allowed ${card.allowedActions} • Blocked ${card.blockedActions} • Escalated ${card.approvalEscalations}",
                    color = TowerTextMuted,
                    fontSize = 12.sp
                )
                Text(
                    text = "First attempt ${(card.firstAttemptSuccessRate * 100).roundToInt()}% • Recovery ${(card.recoverySuccessRate * 100).roundToInt()}% • Confidence ${(card.confidence * 100).roundToInt()}%",
                    color = TowerTextMuted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
            }
        }
    }
}

@Composable
private fun RunCard(run: AgentRunDto, onOpen: () -> Unit) {
    TowerPanel(elevated = run.status in setOf("NEEDS_HUMAN", "BLOCKED", "FAILED")) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = run.status, color = runStatusColor(run.status))
            StatusBadge(label = "STEP ${run.currentStepIndex + 1}", color = TowerAccent)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.sm))
        Text(
            text = run.objective.ifBlank { "Agent execution run" },
            color = TowerTextPrimary,
            fontSize = 18.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 23.sp
        )
        run.repository?.let {
            Text("Repo: $it", color = TowerTextMuted, fontSize = 12.sp)
        }
        Text("Correlation: ${run.correlationId}", color = TowerTextMuted, fontSize = 12.sp)
        run.leaseOwner?.let {
            Text("Worker: $it", color = TowerTextMuted, fontSize = 12.sp)
        }
        run.lastError?.let {
            Text("Blocker: $it", color = RiskCritical, fontSize = 12.sp, lineHeight = 17.sp)
        }
        OutlinedButton(onClick = onOpen, modifier = Modifier.fillMaxWidth()) {
            Text("Open run timeline")
        }
    }
}

@Composable
private fun RunDetailPanel(
    detail: AgentRunDetailDto,
    actionInProgress: Boolean,
    onPause: () -> Unit,
    onResume: () -> Unit,
    onCancel: () -> Unit,
    onRetry: () -> Unit,
    onExecuteNext: () -> Unit,
    onClose: () -> Unit
) {
    val run = detail.run
    val stepInFlight = run.leaseOwner != null || detail.steps.any { it.status == "RUNNING" }

    TowerHeroPanel {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = run.status, color = runStatusColor(run.status))
            StatusBadge(
                label = "${detail.steps.count { it.status == "SUCCEEDED" }}/${detail.steps.size} STEPS",
                color = TowerAccent
            )
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            run.objective,
            color = TowerTextPrimary,
            fontSize = 20.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text("Run ${run.id}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Task ${run.taskId}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Correlation ${run.correlationId}", color = TowerTextMuted, fontSize = 12.sp)
        run.finalEvidenceHash?.let {
            Text("Evidence ${it.take(16)}…", color = RiskLow, fontSize = 12.sp)
        }
        Spacer(modifier = Modifier.height(TowerSpacing.md))

        detail.steps.forEach { step -> StepRow(step) }

        Spacer(modifier = Modifier.height(TowerSpacing.md))
        Text(
            "Latest flight-recorder events",
            color = TowerTextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold
        )
        detail.events.takeLast(6).reversed().forEach { event ->
            TowerPanel(elevated = false) {
                StatusBadge(label = event.eventType, color = TowerAccent)
                Text(
                    event.message.ifBlank { event.reason.ifBlank { "Event recorded" } },
                    color = TowerTextMuted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
                Text(
                    "${event.actor} • ${event.createdAt ?: "recent"}",
                    color = TowerTextMuted,
                    fontSize = 11.sp
                )
            }
        }

        when {
            actionInProgress -> {
                Text("Recording operator action…", color = TowerTextMuted, fontSize = 13.sp)
            }
            stepInFlight -> {
                TowerPanel(elevated = false) {
                    StatusBadge(label = "STEP IN FLIGHT", color = TowerAccent)
                    Text(
                        text = "Pause, cancel and retry become available only after the governed step reaches a durable boundary. Refresh to see the latest result.",
                        color = TowerTextMuted,
                        fontSize = 13.sp,
                        lineHeight = 18.sp
                    )
                }
            }
            else -> {
                if (run.status in setOf("SCHEDULED", "RUNNING", "RETRY_WAIT")) {
                    Button(
                        onClick = onExecuteNext,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Execute next governed step")
                    }
                }
                if (run.status in setOf("SCHEDULED", "RUNNING", "RETRY_WAIT")) {
                    OutlinedButton(onClick = onPause, modifier = Modifier.fillMaxWidth()) {
                        Text("Pause")
                    }
                }
                if (run.status in setOf("PAUSED", "NEEDS_HUMAN", "RETRY_WAIT")) {
                    Button(onClick = onResume, modifier = Modifier.fillMaxWidth()) {
                        Text("Resume")
                    }
                }
                if (run.status in setOf("NEEDS_HUMAN", "RETRY_WAIT")) {
                    Button(onClick = onRetry, modifier = Modifier.fillMaxWidth()) {
                        Text("Approve bounded retry")
                    }
                }
                if (run.status !in setOf("SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED")) {
                    OutlinedButton(onClick = onCancel, modifier = Modifier.fillMaxWidth()) {
                        Text("Cancel run")
                    }
                }
            }
        }
        OutlinedButton(onClick = onClose, modifier = Modifier.fillMaxWidth()) {
            Text("Close run detail")
        }
    }
}

@Composable
private fun StepRow(step: AgentRunStepDto) {
    TowerPanel(elevated = step.status in setOf("NEEDS_HUMAN", "BLOCKED", "FAILED")) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "${step.sequence + 1}", color = TowerAccent)
            StatusBadge(label = step.status, color = runStatusColor(step.status))
            step.decision?.let {
                StatusBadge(label = it, color = runStatusColor(step.status))
            }
        }
        Text(
            step.stepType.replace('_', ' '),
            color = TowerTextPrimary,
            fontSize = 14.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            "Attempts ${step.attemptCount}/${step.maxAttempts}",
            color = TowerTextMuted,
            fontSize = 12.sp
        )
        if (step.reason.isNotBlank()) {
            Text(
                step.reason,
                color = TowerTextMuted,
                fontSize = 12.sp,
                lineHeight = 17.sp
            )
        }
        step.outputReference?.let {
            Text("Output: $it", color = RiskLow, fontSize = 12.sp, lineHeight = 17.sp)
        }
    }
}

private fun exceptionColor(severity: String): Color = when (severity) {
    "CRITICAL", "BLOCKED" -> RiskCritical
    "HIGH" -> RiskHigh
    "MEDIUM" -> RiskMedium
    else -> RiskLow
}

private fun rateColor(rate: Double): Color = when {
    rate >= 0.8 -> RiskLow
    rate >= 0.5 -> RiskMedium
    else -> RiskHigh
}

private fun runStatusColor(status: String): Color = when (status) {
    "SUCCEEDED", "PASS" -> RiskLow
    "RUNNING", "EVALUATING", "READY", "SCHEDULED" -> TowerAccent
    "RETRY_WAIT", "PAUSED", "NEEDS_HUMAN", "NEEDS_REVISION" -> RiskHigh
    "BLOCKED", "FAILED", "FAIL_PERMANENT", "BLOCK_POLICY" -> RiskCritical
    "CANCELLED", "SKIPPED" -> TowerTextMuted
    else -> RiskMedium
}
