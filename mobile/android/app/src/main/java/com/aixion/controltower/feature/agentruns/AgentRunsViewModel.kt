package com.aixion.controltower.feature.agentruns

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.AgentRunsApiClient
import com.aixion.controltower.core.api.dto.AgentReliabilityScorecardDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunDto
import com.aixion.controltower.core.api.dto.AgentRunSummaryDto
import com.aixion.controltower.core.api.dto.RelayHostDto
import com.aixion.controltower.core.api.dto.RelaySessionCreateDto
import com.aixion.controltower.core.api.dto.RelaySessionDetailDto
import com.aixion.controltower.core.api.dto.RelaySessionDto
import com.aixion.controltower.core.api.dto.RelaySummaryDto
import com.aixion.controltower.core.api.dto.TrustExceptionDto
import com.aixion.controltower.data.repository.AgentRunsRepository
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

private data class MissionControlSnapshot(
    val runs: List<AgentRunDto>,
    val summary: AgentRunSummaryDto,
    val exceptions: List<TrustExceptionDto>,
    val scorecards: List<AgentReliabilityScorecardDto>,
    val relayHosts: List<RelayHostDto>,
    val relaySummary: RelaySummaryDto,
    val relaySessions: List<RelaySessionDto>
)

data class AgentRunsUiState(
    val loading: Boolean = true,
    val actionInProgress: Boolean = false,
    val decidingActionId: String? = null,
    val relayActionInProgress: Boolean = false,
    val runs: List<AgentRunDto> = emptyList(),
    val summary: AgentRunSummaryDto = AgentRunSummaryDto(),
    val trustExceptions: List<TrustExceptionDto> = emptyList(),
    val reliabilityScorecards: List<AgentReliabilityScorecardDto> = emptyList(),
    val relayHosts: List<RelayHostDto> = emptyList(),
    val relaySummary: RelaySummaryDto = RelaySummaryDto(),
    val relaySessions: List<RelaySessionDto> = emptyList(),
    val selected: AgentRunDetailDto? = null,
    val selectedRelaySession: RelaySessionDetailDto? = null,
    val errorMessage: String? = null,
    val actionMessage: String? = null
)

class AgentRunsViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AgentRunsRepository(
        AgentRunsApiClient.create(application.applicationContext)
    )
    private val _state = MutableStateFlow(AgentRunsUiState())
    val state: StateFlow<AgentRunsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, errorMessage = null)
            runCatching {
                val runs = async { repository.listRuns() }
                val summary = async { repository.getSummary() }
                val exceptions = async { repository.listTrustExceptions() }
                val scorecards = async { repository.listReliabilityScorecards() }
                val relayHosts = async { repository.listRelayHosts() }
                val relaySummary = async { repository.getRelaySummary() }
                val relaySessions = async { repository.listRelaySessions() }
                MissionControlSnapshot(
                    runs = runs.await(),
                    summary = summary.await(),
                    exceptions = exceptions.await(),
                    scorecards = scorecards.await(),
                    relayHosts = relayHosts.await(),
                    relaySummary = relaySummary.await(),
                    relaySessions = relaySessions.await()
                )
            }.onSuccess { snapshot ->
                val selectedId = _state.value.selected?.run?.id
                val selectedRelayId = _state.value.selectedRelaySession?.session?.id
                _state.value = _state.value.copy(
                    loading = false,
                    runs = snapshot.runs,
                    summary = snapshot.summary,
                    trustExceptions = snapshot.exceptions,
                    reliabilityScorecards = snapshot.scorecards,
                    relayHosts = snapshot.relayHosts,
                    relaySummary = snapshot.relaySummary,
                    relaySessions = snapshot.relaySessions,
                    errorMessage = null,
                    selected = _state.value.selected?.takeIf { detail ->
                        snapshot.runs.any { it.id == detail.run.id }
                    },
                    selectedRelaySession = _state.value.selectedRelaySession?.takeIf { detail ->
                        snapshot.relaySessions.any { it.id == detail.session.id }
                    }
                )
                selectedId?.let(::openRun)
                selectedRelayId?.let(::openRelaySession)
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    runs = emptyList(),
                    trustExceptions = emptyList(),
                    reliabilityScorecards = emptyList(),
                    relayHosts = emptyList(),
                    relaySessions = emptyList(),
                    errorMessage = error.message ?: "Unable to load Mission Control truth."
                )
            }
        }
    }

    fun openRun(runId: String) {
        viewModelScope.launch {
            runCatching { repository.getRun(runId) }
                .onSuccess { detail ->
                    _state.value = _state.value.copy(
                        selected = detail,
                        errorMessage = null
                    )
                }
                .onFailure { error ->
                    _state.value = _state.value.copy(
                        errorMessage = error.message ?: "Unable to load run detail."
                    )
                }
        }
    }

    fun closeRun() {
        _state.value = _state.value.copy(selected = null, actionMessage = null)
    }

    fun openRelaySession(sessionId: String) {
        viewModelScope.launch {
            runCatching { repository.getRelaySession(sessionId) }
                .onSuccess { detail ->
                    _state.value = _state.value.copy(
                        selectedRelaySession = detail,
                        errorMessage = null
                    )
                }
                .onFailure { error ->
                    _state.value = _state.value.copy(
                        errorMessage = error.message ?: "Unable to load agent session."
                    )
                }
        }
    }

    fun closeRelaySession() {
        _state.value = _state.value.copy(
            selectedRelaySession = null,
            actionMessage = null
        )
    }

    fun createRelaySession(
        relayId: String,
        provider: String,
        adapterId: String,
        objective: String,
        workspacePath: String,
        repositoryName: String,
        approvalMode: String = "STRICT"
    ) {
        if (_state.value.relayActionInProgress) return
        if (relayId.isBlank() || provider.isBlank() || adapterId.isBlank()) {
            _state.value = _state.value.copy(
                errorMessage = "Choose an online relay and available agent adapter."
            )
            return
        }
        if (objective.isBlank() || workspacePath.isBlank()) {
            _state.value = _state.value.copy(
                errorMessage = "Objective and absolute workspace path are required."
            )
            return
        }
        viewModelScope.launch {
            _state.value = _state.value.copy(
                relayActionInProgress = true,
                errorMessage = null,
                actionMessage = null
            )
            runCatching {
                repository.createRelaySession(
                    RelaySessionCreateDto(
                        relayId = relayId,
                        provider = provider,
                        adapterId = adapterId,
                        objective = objective.trim(),
                        workspacePath = workspacePath.trim(),
                        repository = repositoryName.trim().ifBlank { null },
                        approvalMode = approvalMode
                    )
                )
            }.onSuccess { detail ->
                _state.value = _state.value.copy(
                    relayActionInProgress = false,
                    selectedRelaySession = detail,
                    actionMessage = "${detail.session.provider} session queued",
                    errorMessage = null
                )
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    relayActionInProgress = false,
                    errorMessage = error.message ?: "Unable to start agent session."
                )
            }
        }
    }

    fun sendRelayMessage(message: String) = relayAct("Instruction queued") { sessionId ->
        if (message.isBlank()) error("Instruction cannot be empty.")
        repository.sendRelayMessage(sessionId, message.trim())
    }

    fun pauseRelaySession() = relayAct("Agent session pause queued") { sessionId ->
        repository.pauseRelaySession(
            sessionId,
            "Paused from Android Aixion Control Tower."
        )
    }

    fun resumeRelaySession() = relayAct("Agent session resume queued") { sessionId ->
        repository.resumeRelaySession(
            sessionId,
            "Resumed from Android Aixion Control Tower."
        )
    }

    fun cancelRelaySession() = relayAct("Agent session cancellation queued") { sessionId ->
        repository.cancelRelaySession(
            sessionId,
            "Cancelled from Android Aixion Control Tower."
        )
    }

    fun decideExactAction(actionId: String, allow: Boolean) {
        if (_state.value.decidingActionId != null) return
        viewModelScope.launch {
            _state.value = _state.value.copy(
                decidingActionId = actionId,
                errorMessage = null,
                actionMessage = null
            )
            val decision = if (allow) "ALLOW" else "BLOCK"
            val reason = if (allow) {
                "Approved this exact immutable action from Android Mission Control."
            } else {
                "Denied this exact immutable action from Android Mission Control."
            }
            runCatching {
                repository.decideExactAction(actionId, decision, reason)
            }.onSuccess {
                _state.value = _state.value.copy(
                    decidingActionId = null,
                    actionMessage = if (allow) {
                        "Exact agent action approved"
                    } else {
                        "Exact agent action blocked"
                    },
                    errorMessage = null
                )
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    decidingActionId = null,
                    errorMessage = error.message ?: "Unable to record exact action decision."
                )
            }
        }
    }

    fun pause() = act("Run paused") { runId ->
        repository.pause(runId, "Paused from Android Mission Control.")
    }

    fun resume() = act("Run resumed") { runId ->
        repository.resume(runId, "Resumed from Android Mission Control.")
    }

    fun cancel() = act("Run cancelled") { runId ->
        repository.cancel(runId, "Cancelled from Android Mission Control.")
    }

    fun retryCurrentStep() = act("Retry approved") { runId ->
        val stepId = _state.value.selected?.run?.currentStepId
        repository.retry(
            runId,
            stepId,
            "Bounded retry approved from Android Mission Control."
        )
    }

    fun executeNext() = act("Next step executed") { runId ->
        repository.executeNext(runId)
    }

    private fun relayAct(
        successMessage: String,
        block: suspend (String) -> Any
    ) {
        val sessionId = _state.value.selectedRelaySession?.session?.id ?: return
        if (_state.value.relayActionInProgress) return
        viewModelScope.launch {
            _state.value = _state.value.copy(
                relayActionInProgress = true,
                errorMessage = null,
                actionMessage = null
            )
            runCatching { block(sessionId) }
                .onSuccess {
                    _state.value = _state.value.copy(
                        relayActionInProgress = false,
                        actionMessage = successMessage,
                        errorMessage = null
                    )
                    refresh()
                }
                .onFailure { error ->
                    _state.value = _state.value.copy(
                        relayActionInProgress = false,
                        errorMessage = error.message ?: "Agent session action failed."
                    )
                }
        }
    }

    private fun act(
        successMessage: String,
        block: suspend (String) -> AgentRunDetailDto
    ) {
        val runId = _state.value.selected?.run?.id ?: return
        viewModelScope.launch {
            _state.value = _state.value.copy(
                actionInProgress = true,
                errorMessage = null,
                actionMessage = null
            )
            runCatching { block(runId) }
                .onSuccess { detail ->
                    _state.value = _state.value.copy(
                        actionInProgress = false,
                        selected = detail,
                        runs = _state.value.runs.map {
                            if (it.id == detail.run.id) detail.run else it
                        },
                        actionMessage = successMessage,
                        errorMessage = null
                    )
                    refresh()
                }
                .onFailure { error ->
                    _state.value = _state.value.copy(
                        actionInProgress = false,
                        errorMessage = error.message ?: "Mission Control action failed."
                    )
                }
        }
    }
}
