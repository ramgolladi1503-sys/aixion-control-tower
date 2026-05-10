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
    val recoveringPendingId: String? = null,
    val message: String? = null,
    val errorMessage: String? = null,
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
            _state.value = _state.value.copy(loading = true, errorMessage = null)
            runCatching {
                val health = repository.getHealth()
                val pending = repository.listPendingRequests()
                health to pending
            }.onSuccess { (health, pending) ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = null,
                    health = health,
                    pendingRequests = pending
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = error.message ?: "Unable to load MCP queue from backend.",
                    health = null,
                    pendingRequests = emptyList()
                )
            }
        }
    }

    fun recoverPendingRequest(pendingRequestId: String) {
        viewModelScope.launch {
            _state.value = _state.value.copy(
                recoveringPendingId = pendingRequestId,
                message = "Recovering MCP pending request...",
                errorMessage = null
            )
            runCatching {
                repository.recoverPendingRequest(pendingRequestId)
            }.onSuccess {
                val health = repository.getHealth()
                val pending = repository.listPendingRequests()
                _state.value = _state.value.copy(
                    recoveringPendingId = null,
                    message = "MCP pending request queued for recovery.",
                    errorMessage = null,
                    health = health,
                    pendingRequests = pending
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    recoveringPendingId = null,
                    errorMessage = error.message ?: "MCP recovery failed."
                )
            }
        }
    }
}
