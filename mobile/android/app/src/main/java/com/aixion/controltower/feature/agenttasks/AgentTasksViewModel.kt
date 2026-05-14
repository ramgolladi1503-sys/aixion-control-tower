package com.aixion.controltower.feature.agenttasks

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.AgentTasksApiClient
import com.aixion.controltower.core.model.AgentTaskEventSummary
import com.aixion.controltower.core.model.AgentTaskSummary
import com.aixion.controltower.data.repository.AgentTasksRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class AgentTasksUiState(
    val loading: Boolean = true,
    val selectedTaskId: String? = null,
    val pendingDeepLinkTaskId: String? = null,
    val errorMessage: String? = null,
    val tasks: List<AgentTaskSummary> = emptyList(),
    val selectedEvents: List<AgentTaskEventSummary> = emptyList()
) {
    val selectedTask: AgentTaskSummary?
        get() = tasks.firstOrNull { task -> task.id == selectedTaskId }

    val waitingForApprovalCount: Int
        get() = tasks.count { task -> task.status.name == "WAITING_FOR_APPROVAL" }

    val activeCount: Int
        get() = tasks.count { task ->
            task.status.name == "RECEIVED" ||
                task.status.name == "PLANNING" ||
                task.status.name == "WAITING_FOR_APPROVAL" ||
                task.status.name == "APPROVED" ||
                task.status.name == "EXECUTING" ||
                task.status.name == "TESTING"
        }
}

class AgentTasksViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AgentTasksRepository(AgentTasksApiClient.create(application.applicationContext))

    private val _state = MutableStateFlow(AgentTasksUiState())
    val state: StateFlow<AgentTasksUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun openFromDeepLink(taskId: String) {
        val current = _state.value
        if (current.selectedTaskId == taskId) return
        _state.value = current.copy(selectedTaskId = taskId, pendingDeepLinkTaskId = taskId, selectedEvents = emptyList())
        if (current.tasks.any { it.id == taskId }) {
            loadEvents(taskId)
        } else {
            refresh()
        }
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, errorMessage = null)
            runCatching {
                repository.listAgentTasks()
            }.onSuccess { tasks ->
                val pendingDeepLinkTaskId = _state.value.pendingDeepLinkTaskId
                val selectedTaskId = when {
                    pendingDeepLinkTaskId != null && tasks.any { it.id == pendingDeepLinkTaskId } -> pendingDeepLinkTaskId
                    _state.value.selectedTaskId != null && tasks.any { it.id == _state.value.selectedTaskId } -> _state.value.selectedTaskId
                    else -> null
                }
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = null,
                    tasks = tasks,
                    selectedTaskId = selectedTaskId,
                    pendingDeepLinkTaskId = null,
                    selectedEvents = if (selectedTaskId == null) emptyList() else _state.value.selectedEvents
                )
                selectedTaskId?.let { loadEvents(it) }
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = error.message ?: "Unable to load agent tasks.",
                    tasks = emptyList(),
                    selectedTaskId = null,
                    selectedEvents = emptyList()
                )
            }
        }
    }

    fun selectTask(taskId: String) {
        _state.value = _state.value.copy(selectedTaskId = taskId, pendingDeepLinkTaskId = null, selectedEvents = emptyList())
        loadEvents(taskId)
    }

    fun clearSelection() {
        _state.value = _state.value.copy(selectedTaskId = null, pendingDeepLinkTaskId = null, selectedEvents = emptyList())
    }

    private fun loadEvents(taskId: String) {
        viewModelScope.launch {
            runCatching {
                repository.listAgentTaskEvents(taskId)
            }.onSuccess { events ->
                _state.value = _state.value.copy(selectedEvents = events, errorMessage = null)
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    errorMessage = error.message ?: "Unable to load agent task timeline.",
                    selectedEvents = emptyList()
                )
            }
        }
    }
}
