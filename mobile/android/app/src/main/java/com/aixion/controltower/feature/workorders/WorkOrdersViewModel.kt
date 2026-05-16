package com.aixion.controltower.feature.workorders

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.WorkOrderSummary
import com.aixion.controltower.data.repository.ProjectRepository
import com.aixion.controltower.data.repository.WorkOrderRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class WorkOrdersUiState(
    val loading: Boolean = true,
    val workOrders: List<WorkOrderSummary> = emptyList(),
    val errorMessage: String? = null
) {
    val hasError: Boolean = errorMessage != null
}

class WorkOrdersViewModel(application: Application) : AndroidViewModel(application) {
    private val api = ApiClient.create(application.applicationContext)
    private val projectRepository = ProjectRepository(api)
    private val workOrderRepository = WorkOrderRepository(api)

    private val _state = MutableStateFlow(WorkOrdersUiState())
    val state: StateFlow<WorkOrdersUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, errorMessage = null)
            runCatching {
                val projects = projectRepository.listProjects()
                val projectNames = projects.associate { it.id to it.name }
                workOrderRepository.listWorkOrders(projectNames)
            }.onSuccess { workOrders ->
                _state.value = WorkOrdersUiState(
                    loading = false,
                    workOrders = workOrders
                )
            }.onFailure { error ->
                _state.value = WorkOrdersUiState(
                    loading = false,
                    errorMessage = "Backend work-order sync failed. No mock work orders shown. ${error.message ?: "Retry when the backend is reachable."}"
                )
            }
        }
    }
}