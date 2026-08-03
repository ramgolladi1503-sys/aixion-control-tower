package com.aixion.controltower.feature.agentruns

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.AgentRunsApiClient
import com.aixion.controltower.core.api.dto.AgentReliabilityScorecardDto
import com.aixion.controltower.core.api.dto.AgentRunDetailDto
import com.aixion.controltower.core.api.dto.AgentRunDto
import com.aixion.controltower.core.api.dto.AgentRunSummaryDto
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
    val scorecards: List<AgentReliabilityScorecardDto>
)

data class AgentRunsUiState(
    val loading: Boolean = true,
    val actionInProgress: Boolean = false,
    val runs: List<AgentRunDto> = emptyList(),
    val summary: AgentRunSummaryDto = AgentRunSummaryDto(),
    val trustExceptions: List<TrustExceptionDto> = emptyList(),
    val reliabilityScorecards: List<AgentReliabilityScorecardDto> = emptyList(),
    val selected: AgentRunDetailDto? = null,
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
                MissionControlSnapshot(
                    runs = runs.await(),
                    summary = summary.await(),
                    exceptions = exceptions.await(),
                    scorecards = scorecards.await()
                )
            }.onSuccess { snapshot ->
                val selectedId = _state.value.selected?.run?.id
                _state.value = _state.value.copy(
                    loading = false,
                    runs = snapshot.runs,
                    summary = snapshot.summary,
                    trustExceptions = snapshot.exceptions,
                    reliabilityScorecards = snapshot.scorecards,
                    errorMessage = null,
                    selected = _state.value.selected?.takeIf { detail ->
                        snapshot.runs.any { it.id == detail.run.id }
                    }
                )
                selectedId?.let(::openRun)
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    runs = emptyList(),
                    trustExceptions = emptyList(),
                    reliabilityScorecards = emptyList(),
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
