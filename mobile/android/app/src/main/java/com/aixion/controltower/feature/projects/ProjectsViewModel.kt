package com.aixion.controltower.feature.projects

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.data.repository.ProjectRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class ProjectsUiState(
    val loading: Boolean = true,
    val projects: List<ProjectSummary> = emptyList()
)

class ProjectsViewModel : ViewModel() {
    private val repository = ProjectRepository(ApiClient.create())

    private val _state = MutableStateFlow(ProjectsUiState())
    val state: StateFlow<ProjectsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = ProjectsUiState(loading = false, projects = repository.listProjects())
        }
    }
}
