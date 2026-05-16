package com.aixion.controltower.feature.approvals

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.data.repository.ApprovalRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class ApprovalsUiState(
    val loading: Boolean = true,
    val approvals: List<ApprovalSummary> = emptyList(),
    val selectedApproval: ApprovalSummary? = null,
    val lastActionMessage: String? = null,
    val errorMessage: String? = null
) {
    val hasError: Boolean = errorMessage != null
}

class ApprovalsViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = ApprovalRepository(ApiClient.create(application.applicationContext))

    private val _state = MutableStateFlow(ApprovalsUiState())
    val state: StateFlow<ApprovalsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, errorMessage = null)
            runCatching {
                repository.listApprovals()
            }.onSuccess { approvals ->
                _state.value = ApprovalsUiState(
                    loading = false,
                    approvals = approvals,
                    selectedApproval = approvals.firstOrNull()
                )
            }.onFailure { error ->
                _state.value = ApprovalsUiState(
                    loading = false,
                    errorMessage = "Backend approval sync failed. No mock approvals shown. ${error.message ?: "Retry when the backend is reachable."}"
                )
            }
        }
    }

    fun selectApproval(approval: ApprovalSummary) {
        _state.value = _state.value.copy(selectedApproval = approval)
    }

    fun openApprovalById(approvalId: String) {
        viewModelScope.launch {
            val cachedApproval = _state.value.approvals.firstOrNull { it.id == approvalId }
            if (cachedApproval != null) {
                _state.value = _state.value.copy(
                    loading = false,
                    selectedApproval = cachedApproval,
                    lastActionMessage = "Opened linked approval from MCP Queue"
                )
                return@launch
            }

            runCatching {
                repository.getApproval(approvalId)
            }.onSuccess { selected ->
                _state.value = _state.value.copy(
                    loading = false,
                    selectedApproval = selected,
                    lastActionMessage = "Opened linked approval from MCP Queue"
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    selectedApproval = null,
                    lastActionMessage = "Failed to open linked approval $approvalId: ${error.message ?: "backend request failed"}"
                )
            }
        }
    }

    fun decide(decision: String, reason: String, onCompleted: () -> Unit = {}) {
        val approval = _state.value.selectedApproval ?: return
        viewModelScope.launch {
            _state.value = _state.value.copy(lastActionMessage = "Sending $decision decision...")
            runCatching {
                val updated = repository.decide(approval.id, decision, reason)
                val resolveMessage = if (approval.isMCPToolApproval) {
                    val result = repository.resolveMCPApproval(approval.id)
                    if (result.forwarded) {
                        " MCP request forwarded."
                    } else {
                        " MCP resolve: ${result.reason}"
                    }
                } else {
                    ""
                }
                updated to resolveMessage
            }.onSuccess { (updated, resolveMessage) ->
                val remaining = _state.value.approvals.filterNot { it.id == updated.id }
                _state.value = _state.value.copy(
                    approvals = remaining,
                    selectedApproval = remaining.firstOrNull(),
                    lastActionMessage = "Decision sent: $decision. Request moved out of the active queue.$resolveMessage"
                )
                onCompleted()
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    lastActionMessage = "Decision failed: ${error.message ?: "backend request failed"}"
                )
            }
        }
    }
}
