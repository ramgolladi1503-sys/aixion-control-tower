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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
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
import com.aixion.controltower.core.api.dto.RelayHostDto
import com.aixion.controltower.core.api.dto.RelaySessionDetailDto
import com.aixion.controltower.core.api.dto.RelaySessionDto
import com.aixion.controltower.core.api.dto.RelaySummaryDto
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
                    text = "Universal Agent Control",
                    color = TowerTextPrimary,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.SemiBold
                )
                Text(
                    text = "Start, steer and stop Codex, Claude, Antigravity, OpenClaw and future adapters while every side effect remains inside Aixion trust policy.",
                    color = TowerTextMuted,
                    fontSize = 14.sp,
                    lineHeight = 20.sp
                )
                Spacer(modifier = Modifier.height(TowerSpacing.md))
                Button(onClick = viewModel::refresh, modifier = Modifier.fillMaxWidth()) {
                    Text("Refresh control-plane truth")
                }
            }
        }

        item { RunSummaryPanel(state.summary) }
        item { RelaySummaryPanel(state.relaySummary) }

        if (state.trustExceptions.isNotEmpty()) {
            item {
                TrustExceptionQueuePanel(
                    exceptions = state.trustExceptions,
                    decidingActionId = state.decidingActionId,
                    onApprove = { viewModel.decideExactAction(it, allow = true) },
                    onBlock = { viewModel.decideExactAction(it, allow = false) }
                )
            }
        }

        if (state.relayHosts.isNotEmpty()) {
            item {
                RelayLaunchPanel(
                    hosts = state.relayHosts,
                    busy = state.relayActionInProgress,
                    onStart = viewModel::createRelaySession
                )
            }
        }

        state.selectedRelaySession?.let { detail ->
            item {
                RelaySessionDetailPanel(
                    detail = detail,
                    busy = state.relayActionInProgress,
                    onSend = viewModel::sendRelayMessage,
                    onPause = viewModel::pauseRelaySession,
                    onResume = viewModel::resumeRelaySession,
                    onCancel = viewModel::cancelRelaySession,
                    onClose = viewModel::closeRelaySession
                )
            }
        }

        item {
            TowerSectionHeader(
                title = "Connected agent hosts",
                subtitle = "The relay connects outbound from your Mac or worker. No SSH, shell port or provider credential is exposed to the phone."
            )
        }

        if (!state.loading && state.relayHosts.isEmpty()) {
            item {
                TowerPanel(elevated = true) {
                    StatusBadge(label = "NO RELAY", color = RiskHigh)
                    Text(
                        text = "Register and run aixion-relay on a Mac, Linux worker or Windows host before starting provider sessions.",
                        color = TowerTextMuted,
                        fontSize = 14.sp,
                        lineHeight = 20.sp
                    )
                }
            }
        }

        items(state.relayHosts, key = { it.id }) { relay ->
            RelayHostCard(relay)
        }

        item {
            TowerSectionHeader(
                title = "Agent sessions",
                subtitle = "One provider-neutral timeline for messages, tool calls, tests, approvals, usage, failures and final evidence."
            )
        }

        if (!state.loading && state.relaySessions.isEmpty()) {
            item {
                TowerPanel(elevated = false) {
                    Text(
                        text = "No agent sessions yet. Choose an available adapter above and start a scoped session.",
                        color = TowerTextMuted,
                        fontSize = 13.sp
                    )
                }
            }
        }

        items(state.relaySessions, key = { it.id }) { session ->
            RelaySessionCard(
                session = session,
                onOpen = { viewModel.openRelaySession(session.id) }
            )
        }

        if (state.reliabilityScorecards.isNotEmpty()) {
            item { AgentReliabilityPanel(state.reliabilityScorecards) }
        }

        state.errorMessage?.let { error ->
            item {
                TowerPanel(elevated = true) {
                    StatusBadge(label = "CONTROL ERROR", color = RiskCritical)
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
                title = "Durable run queue",
                subtitle = "Repository-changing work remains governed by signed capability leases, ordered steps, retries and evidence sealing."
            )
        }

        items(state.runs, key = { it.id }) { run ->
            RunCard(run = run, onOpen = { viewModel.openRun(run.id) })
        }
    }
}

@Composable
private fun RunSummaryPanel(summary: AgentRunSummaryDto) {
    TowerPanel(elevated = true) {
        Text(
            text = "Execution truth",
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
        Text(
            text = "Queue ${summary.queueDepth} • Retry ${summary.retryWait} • Blocked ${summary.blocked} • Completed ${summary.succeeded}",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}

@Composable
private fun RelaySummaryPanel(summary: RelaySummaryDto) {
    TowerPanel(elevated = summary.offlineRelays > 0 || summary.failedSessions > 0) {
        Text(
            text = "Agent connectivity",
            color = TowerTextPrimary,
            fontSize = 17.sp,
            fontWeight = FontWeight.SemiBold
        )
        Row(horizontalArrangement = Arrangement.spacedBy(7.dp)) {
            StatusBadge(label = "ONLINE ${summary.onlineRelays}", color = RiskLow)
            StatusBadge(
                label = "OFFLINE ${summary.offlineRelays}",
                color = if (summary.offlineRelays > 0) RiskHigh else RiskLow
            )
            StatusBadge(
                label = "APPROVAL ${summary.waitingForApproval}",
                color = if (summary.waitingForApproval > 0) RiskHigh else RiskLow
            )
        }
        Text(
            text = "Sessions ${summary.totalSessions} • Active ${summary.activeSessions} • Pending commands ${summary.pendingCommands} • Failed ${summary.failedSessions}",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 19.sp
        )
    }
}

@Composable
private fun RelayLaunchPanel(
    hosts: List<RelayHostDto>,
    busy: Boolean,
    onStart: (String, String, String, String, String, String, String) -> Unit
) {
    var selectedRelayId by rememberSaveable { mutableStateOf("") }
    var selectedProvider by rememberSaveable { mutableStateOf("") }
    var selectedAdapterId by rememberSaveable { mutableStateOf("") }
    var objective by rememberSaveable { mutableStateOf("") }
    var workspace by rememberSaveable { mutableStateOf("") }
    var repository by rememberSaveable { mutableStateOf("") }

    val selectedRelay = hosts.firstOrNull { it.id == selectedRelayId }
    TowerHeroPanel {
        StatusBadge(label = "START AGENT", color = TowerAccent)
        Text(
            text = "Choose a connected adapter",
            color = TowerTextPrimary,
            fontSize = 18.sp,
            fontWeight = FontWeight.SemiBold
        )
        hosts.filter { it.status == "ONLINE" }.forEach { relay ->
            relay.adapters.filter { it.available }.forEach { adapter ->
                val selected = relay.id == selectedRelayId && adapter.adapterId == selectedAdapterId
                OutlinedButton(
                    onClick = {
                        selectedRelayId = relay.id
                        selectedProvider = adapter.provider
                        selectedAdapterId = adapter.adapterId
                        if (workspace.isBlank()) {
                            workspace = relay.workspaceRoots.firstOrNull().orEmpty()
                        }
                        if (repository.isBlank()) {
                            repository = relay.allowedRepositories.firstOrNull().orEmpty()
                        }
                    },
                    modifier = Modifier.fillMaxWidth()
                ) {
                    Text(
                        if (selected) {
                            "Selected: ${adapter.displayName} on ${relay.name}"
                        } else {
                            "${adapter.displayName} • ${relay.name}"
                        }
                    )
                }
            }
        }
        if (selectedRelay != null) {
            Text(
                text = "Scope: ${selectedRelay.allowedRepositories.joinToString().ifBlank { "operator-approved repositories" }}",
                color = TowerTextMuted,
                fontSize = 12.sp
            )
        }
        OutlinedTextField(
            value = objective,
            onValueChange = { objective = it },
            label = { Text("Objective") },
            modifier = Modifier.fillMaxWidth(),
            minLines = 3
        )
        OutlinedTextField(
            value = workspace,
            onValueChange = { workspace = it },
            label = { Text("Absolute workspace path") },
            modifier = Modifier.fillMaxWidth()
        )
        OutlinedTextField(
            value = repository,
            onValueChange = { repository = it },
            label = { Text("Repository owner/name") },
            modifier = Modifier.fillMaxWidth()
        )
        Button(
            onClick = {
                onStart(
                    selectedRelayId,
                    selectedProvider,
                    selectedAdapterId,
                    objective,
                    workspace,
                    repository,
                    "STRICT"
                )
            },
            enabled = !busy && selectedAdapterId.isNotBlank(),
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(if (busy) "Queuing session…" else "Start strict governed session")
        }
    }
}

@Composable
private fun RelayHostCard(relay: RelayHostDto) {
    TowerPanel(elevated = relay.status != "ONLINE") {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = relay.status, color = relayStatusColor(relay.status))
            StatusBadge(label = relay.platform, color = TowerAccent)
            StatusBadge(label = "${relay.adapters.count { it.available }} ADAPTERS", color = RiskLow)
        }
        Text(
            text = relay.name,
            color = TowerTextPrimary,
            fontSize = 17.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text("${relay.hostname} • relay ${relay.relayVersion}", color = TowerTextMuted, fontSize = 12.sp)
        Text(
            text = relay.adapters.joinToString { adapter ->
                "${adapter.provider}:${adapter.displayName}${if (adapter.available) "" else " (unavailable)"}"
            },
            color = TowerTextMuted,
            fontSize = 12.sp,
            lineHeight = 17.sp
        )
        relay.lastHeartbeatAt?.let {
            Text("Heartbeat $it", color = TowerTextMuted, fontSize = 11.sp)
        }
    }
}

@Composable
private fun RelaySessionCard(session: RelaySessionDto, onOpen: () -> Unit) {
    TowerPanel(
        elevated = session.status in setOf("WAITING_FOR_APPROVAL", "FAILED")
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = session.provider, color = TowerAccent)
            StatusBadge(label = session.status, color = relayStatusColor(session.status))
            StatusBadge(label = session.approvalMode, color = RiskMedium)
        }
        Text(
            text = session.objective,
            color = TowerTextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold,
            lineHeight = 21.sp
        )
        Text("Adapter ${session.adapterId}", color = TowerTextMuted, fontSize = 12.sp)
        session.repository?.let {
            Text("Repository $it", color = TowerTextMuted, fontSize = 12.sp)
        }
        session.lastError?.let {
            Text("Blocker: $it", color = RiskCritical, fontSize = 12.sp, lineHeight = 17.sp)
        }
        OutlinedButton(onClick = onOpen, modifier = Modifier.fillMaxWidth()) {
            Text("Open agent timeline")
        }
    }
}

@Composable
private fun RelaySessionDetailPanel(
    detail: RelaySessionDetailDto,
    busy: Boolean,
    onSend: (String) -> Unit,
    onPause: () -> Unit,
    onResume: () -> Unit,
    onCancel: () -> Unit,
    onClose: () -> Unit
) {
    var instruction by rememberSaveable(detail.session.id) { mutableStateOf("") }
    val session = detail.session
    val terminal = session.status in setOf("COMPLETED", "FAILED", "CANCELLED")

    TowerHeroPanel {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = session.provider, color = TowerAccent)
            StatusBadge(label = session.status, color = relayStatusColor(session.status))
            StatusBadge(label = "EVENTS ${session.latestEventSequence}", color = RiskLow)
        }
        Text(
            text = session.objective,
            color = TowerTextPrimary,
            fontSize = 20.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text("Host ${detail.relay.name} • ${session.adapterId}", color = TowerTextMuted, fontSize = 12.sp)
        Text("Workspace ${session.workspacePath}", color = TowerTextMuted, fontSize = 12.sp)
        session.remoteSessionId?.let {
            Text("Provider session $it", color = TowerTextMuted, fontSize = 12.sp)
        }
        session.finalEvidenceHash?.let {
            Text("Evidence ${it.take(20)}…", color = RiskLow, fontSize = 12.sp)
        }

        Text(
            text = "Latest normalized events",
            color = TowerTextPrimary,
            fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold
        )
        detail.events.takeLast(10).reversed().forEach { event ->
            TowerPanel(elevated = event.eventType in setOf("APPROVAL_REQUIRED", "SESSION_FAILED")) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusBadge(label = event.eventType, color = relayEventColor(event.eventType))
                    StatusBadge(label = "#${event.sequence}", color = TowerAccent)
                }
                Text(
                    text = event.message.ifBlank { "Provider event recorded" },
                    color = TowerTextMuted,
                    fontSize = 12.sp,
                    lineHeight = 17.sp
                )
                Text("Hash ${event.eventHash.take(12)}…", color = TowerTextMuted, fontSize = 10.sp)
            }
        }

        if (!terminal) {
            OutlinedTextField(
                value = instruction,
                onValueChange = { instruction = it },
                label = { Text("Follow-up instruction") },
                modifier = Modifier.fillMaxWidth(),
                minLines = 2
            )
            Button(
                onClick = {
                    onSend(instruction)
                    instruction = ""
                },
                enabled = !busy && instruction.isNotBlank(),
                modifier = Modifier.fillMaxWidth()
            ) {
                Text("Send instruction")
            }
            if (session.status == "PAUSED") {
                Button(onClick = onResume, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
                    Text("Resume agent")
                }
            } else {
                OutlinedButton(onClick = onPause, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
                    Text("Pause agent")
                }
            }
            OutlinedButton(onClick = onCancel, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
                Text("Cancel agent session")
            }
        }
        OutlinedButton(onClick = onClose, modifier = Modifier.fillMaxWidth()) {
            Text("Close agent timeline")
        }
    }
}

@Composable
private fun TrustExceptionQueuePanel(
    exceptions: List<TrustExceptionDto>,
    decidingActionId: String?,
    onApprove: (String) -> Unit,
    onBlock: (String) -> Unit
) {
    TowerHeroPanel {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusBadge(label = "NEEDS ATTENTION ${exceptions.size}", color = RiskHigh)
            val critical = exceptions.count { it.severity in setOf("CRITICAL", "BLOCKED") }
            StatusBadge(
                label = "CRITICAL $critical",
                color = if (critical > 0) RiskCritical else RiskLow
            )
        }
        Text(
            text = "Exact action queue",
            color = TowerTextPrimary,
            fontSize = 19.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text(
            text = "Approval is bound to the immutable command, paths, repository, branch, network scope and capability lease.",
            color = TowerTextMuted,
            fontSize = 13.sp,
            lineHeight = 18.sp
        )
        exceptions.take(8).forEach { item ->
            val canDecide = item.category == "REQUIRE_APPROVAL" && item.actionId != null
            TowerPanel(elevated = item.severity in setOf("CRITICAL", "BLOCKED")) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusBadge(label = item.category, color = exceptionColor(item.severity))
                    StatusBadge(label = item.severity, color = exceptionColor(item.severity))
                }
                Text(item.title, color = TowerTextPrimary, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                Text(item.summary, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
                item.actionId?.let {
                    Text("Exact action $it", color = TowerTextMuted, fontSize = 11.sp)
                }
                if (canDecide) {
                    Button(
                        onClick = { onApprove(item.actionId!!) },
                        enabled = decidingActionId == null,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (decidingActionId == item.actionId) "Recording…" else "Approve exact action")
                    }
                    OutlinedButton(
                        onClick = { onBlock(item.actionId!!) },
                        enabled = decidingActionId == null,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("Block exact action")
                    }
                }
            }
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
            text = "Scores come from policy decisions, attempts, recovery and sealed evidence—not provider claims.",
            color = TowerTextMuted,
            fontSize = 12.sp,
            lineHeight = 17.sp
        )
        scorecards.take(6).forEach { card ->
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
        Text(run.objective, color = TowerTextPrimary, fontSize = 20.sp, fontWeight = FontWeight.SemiBold)
        Text("Run ${run.id}", color = TowerTextMuted, fontSize = 12.sp)
        run.capabilityLeaseId?.let {
            Text("Capability $it", color = RiskLow, fontSize = 12.sp)
        }
        run.finalEvidenceHash?.let {
            Text("Evidence ${it.take(16)}…", color = RiskLow, fontSize = 12.sp)
        }
        detail.steps.forEach { step -> StepRow(step) }

        when {
            actionInProgress -> Text("Recording operator action…", color = TowerTextMuted, fontSize = 13.sp)
            stepInFlight -> Text(
                "Controls unlock at the next durable step boundary.",
                color = TowerTextMuted,
                fontSize = 13.sp
            )
            else -> {
                if (run.status in setOf("SCHEDULED", "RUNNING", "RETRY_WAIT")) {
                    Button(onClick = onExecuteNext, modifier = Modifier.fillMaxWidth()) {
                        Text("Execute next governed step")
                    }
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
            step.decision?.let { StatusBadge(label = it, color = runStatusColor(step.status)) }
        }
        Text(
            text = step.stepType.replace('_', ' '),
            color = TowerTextPrimary,
            fontSize = 14.sp,
            fontWeight = FontWeight.SemiBold
        )
        Text("Attempts ${step.attemptCount}/${step.maxAttempts}", color = TowerTextMuted, fontSize = 12.sp)
        if (step.reason.isNotBlank()) {
            Text(step.reason, color = TowerTextMuted, fontSize = 12.sp, lineHeight = 17.sp)
        }
    }
}

private fun exceptionColor(severity: String): Color = when (severity) {
    "CRITICAL", "BLOCKED" -> RiskCritical
    "HIGH" -> RiskHigh
    "MEDIUM" -> RiskMedium
    else -> RiskLow
}

private fun relayStatusColor(status: String): Color = when (status) {
    "ONLINE", "RUNNING", "COMPLETED", "SESSION_COMPLETED" -> RiskLow
    "QUEUED", "STARTING" -> TowerAccent
    "WAITING_FOR_APPROVAL", "PAUSED", "OFFLINE", "DEGRADED" -> RiskHigh
    "FAILED", "CANCELLED", "DISABLED" -> RiskCritical
    else -> RiskMedium
}

private fun relayEventColor(eventType: String): Color = when (eventType) {
    "SESSION_COMPLETED", "APPROVAL_RESOLVED", "TEST_RESULT" -> RiskLow
    "APPROVAL_REQUIRED" -> RiskHigh
    "SESSION_FAILED", "SESSION_CANCELLED" -> RiskCritical
    else -> TowerAccent
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
