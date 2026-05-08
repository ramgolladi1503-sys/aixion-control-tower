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

    fun decide(decision: String, reason: String) {
        val approval = _state.value.selectedApproval ?: return
        viewModelScope.launch {
            val updated = repository.decide(approval.id, decision, reason)
            _state.value = _state.value.copy(
                selectedApproval = updated,
                lastActionMessage = "Decision sent: $decision"
            )
        }
    }
}
