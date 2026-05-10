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
    val workOrders: List<WorkOrderSummary> = emptyList()
)

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
            val projects = projectRepository.listProjects()
            val projectNames = projects.associate { it.id to it.name }
            _state.value = WorkOrdersUiState(
                loading = false,
                workOrders = workOrderRepository.listWorkOrders(projectNames)
            )
        }
    }
}
