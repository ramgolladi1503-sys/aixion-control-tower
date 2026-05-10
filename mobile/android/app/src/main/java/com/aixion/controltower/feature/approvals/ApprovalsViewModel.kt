package com.aixion.controltower.feature.approvals

import androidx.lifecycle.ViewModel
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
    val lastActionMessage: String? = null
)

class ApprovalsViewModel : ViewModel() {
    private val repository = ApprovalRepository(ApiClient.create())

    private val _state = MutableStateFlow(ApprovalsUiState())
    val state: StateFlow<ApprovalsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            val approvals = repository.listApprovals()
            _state.value = ApprovalsUiState(
                loading = false,
                approvals = approvals,
                selectedApproval = approvals.firstOrNull()
            )
        }
    }

    fun selectApproval(approval: ApprovalSummary) {
        _state.value = _state.value.copy(selectedApproval = approval)
    }

    fun openApprovalById(approvalId: String) {
        viewModelScope.launch {
            val cachedApproval = _state.value.approvals.firstOrNull { it.id == approvalId }
            val selected = cachedApproval ?: repository.getApproval(approvalId)
            _state.value = _state.value.copy(
                loading = false,
                selectedApproval = selected,
                lastActionMessage = "Opened linked approval from MCP Queue"
            )
        }
    }

    fun decide(decision: String, reason: String, onCompleted: () -> Unit = {}) {
        val approval = _state.value.selectedApproval ?: return
        viewModelScope.launch {
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
                _state.value = _state.value.copy(
                    selectedApproval = updated,
                    lastActionMessage = "Decision sent: $decision.$resolveMessage"
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
