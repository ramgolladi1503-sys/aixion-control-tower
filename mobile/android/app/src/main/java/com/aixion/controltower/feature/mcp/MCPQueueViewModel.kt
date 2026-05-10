package com.aixion.controltower.feature.mcp

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.MCPPendingHealthSummary
import com.aixion.controltower.core.model.MCPPendingSummary
import com.aixion.controltower.data.repository.MCPPendingRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class MCPQueueUiState(
    val loading: Boolean = true,
    val health: MCPPendingHealthSummary? = null,
    val pendingRequests: List<MCPPendingSummary> = emptyList()
)

class MCPQueueViewModel : ViewModel() {
    private val repository = MCPPendingRepository(ApiClient.create())

    private val _state = MutableStateFlow(MCPQueueUiState())
    val state: StateFlow<MCPQueueUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            val health = repository.getHealth()
            val pending = repository.listPendingRequests()
            _state.value = MCPQueueUiState(
                loading = false,
                health = health,
                pendingRequests = pending
            )
        }
    }
}
