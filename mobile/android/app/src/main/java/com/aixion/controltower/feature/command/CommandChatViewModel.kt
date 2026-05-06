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
    val message: String? = null
)

class CommandChatViewModel : ViewModel() {
    private val api = ApiClient.create()
    private val projectRepository = ProjectRepository(api)
    private val commandRepository = CommandRepository(api)

    private val _state = MutableStateFlow(CommandUiState())
    val state: StateFlow<CommandUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            val projects = projectRepository.listProjects()
            _state.value = _state.value.copy(
                projects = projects,
                selectedProject = projects.firstOrNull()
            )
        }
    }

    fun selectProject(project: ProjectSummary) {
        _state.value = _state.value.copy(selectedProject = project)
    }

    fun createWorkOrder(rawCommand: String) {
        if (rawCommand.isBlank()) return
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, message = "Creating controlled work order...")
            val workOrder = commandRepository.createControlledWorkOrder(
                project = _state.value.selectedProject,
                title = rawCommand.take(48),
                rawCommand = rawCommand
            )
            _state.value = _state.value.copy(
                loading = false,
                generatedWorkOrder = workOrder,
                message = "Work order ready: ${workOrder.goal.take(48)}"
            )
        }
    }
}
