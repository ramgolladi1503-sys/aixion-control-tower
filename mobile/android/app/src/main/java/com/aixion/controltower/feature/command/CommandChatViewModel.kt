package com.aixion.controltower.feature.command

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.WorkOrderSummary
import com.aixion.controltower.data.repository.CommandRepository
import com.aixion.controltower.data.repository.ProjectRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class CommandUiState(
    val loading: Boolean = false,
    val projects: List<ProjectSummary> = emptyList(),
    val selectedProject: ProjectSummary? = null,
    val generatedWorkOrder: WorkOrderSummary? = null,
    val message: String? = null,
    val errorMessage: String? = null
)

class CommandChatViewModel : ViewModel() {
    private val api = ApiClient.create()
    private val projectRepository = ProjectRepository(api)
    private val commandRepository = CommandRepository(api)

    private val _state = MutableStateFlow(CommandUiState())
    val state: StateFlow<CommandUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            runCatching { projectRepository.listProjects() }
                .onSuccess { projects ->
                    _state.value = _state.value.copy(
                        projects = projects,
                        selectedProject = projects.firstOrNull(),
                        errorMessage = null
                    )
                }
                .onFailure { error ->
                    _state.value = _state.value.copy(
                        projects = emptyList(),
                        selectedProject = null,
                        errorMessage = error.message ?: "Unable to load projects. Command needs backend project data before it can create a real Work Order."
                    )
                }
        }
    }

    fun selectProject(project: ProjectSummary) {
        _state.value = _state.value.copy(selectedProject = project)
    }

    fun createWorkOrder(rawCommand: String) {
        if (rawCommand.isBlank()) return
        viewModelScope.launch {
            _state.value = _state.value.copy(
                loading = true,
                message = "Creating Work Order...",
                errorMessage = null,
                generatedWorkOrder = null
            )
            runCatching {
                commandRepository.createControlledWorkOrder(
                    project = _state.value.selectedProject,
                    title = rawCommand.take(48),
                    rawCommand = rawCommand
                )
            }.onSuccess { workOrder ->
                _state.value = _state.value.copy(
                    loading = false,
                    generatedWorkOrder = workOrder,
                    message = "Work Order created. Review it in Work Orders before approval or execution.",
                    errorMessage = null
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    generatedWorkOrder = null,
                    message = null,
                    errorMessage = error.message ?: "Work Order was not created. Backend sync is required."
                )
            }
        }
    }
}