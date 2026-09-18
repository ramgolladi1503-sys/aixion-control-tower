package com.aixion.controltower.feature.approvals

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.AgentRunsApiClient
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.TrustExceptionDto
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.data.repository.AgentRunsRepository
import com.aixion.controltower.data.repository.ApprovalRepository
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class ApprovalsUiState(
    val loading: Boolean = true,
    val approvals: List<ApprovalSummary> = emptyList(),
    val nativeActions: List<TrustExceptionDto> = emptyList(),
    val selectedApproval: ApprovalSummary? = null,
    val decidingNativeActionId: String? = null,
    val lastActionMessage: String? = null,
    val errorMessage: String? = null
) {
    val pendingNativeActions: List<TrustExceptionDto> =
        nativeActions.filter { it.isPendingExactAction }
    val blockedNativeActions: List<TrustExceptionDto> =
        nativeActions.filter { it.category == "BLOCK" && it.actionId != null }
    val hasError: Boolean = errorMessage != null
}

class ApprovalsViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = ApprovalRepository(ApiClient.create(application.applicationContext))
    private val agentRunsRepository = AgentRunsRepository(
        AgentRunsApiClient.create(application.applicationContext)
    )
    private val refreshMutex = Mutex()

    private val _state = MutableStateFlow(ApprovalsUiState())
    val state: StateFlow<ApprovalsUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            load(showLoading = true)
            while (isActive) {
                delay(3_000)
                load(showLoading = false)
            }
        }
    }

    fun refresh() {
        viewModelScope.launch { load(showLoading = true) }
    }

    private suspend fun load(showLoading: Boolean) {
        refreshMutex.withLock {
            if (showLoading) {
                _state.value = _state.value.copy(loading = true, errorMessage = null)
            }
            val selectedId = _state.value.selectedApproval?.id
            runCatching {
                repository.listApprovals() to agentRunsRepository.listTrustExceptions()
            }.onSuccess { (approvals, nativeActions) ->
                _state.value = _state.value.copy(
                    loading = false,
                    approvals = approvals,
                    nativeActions = nativeActions,
                    selectedApproval = approvals.firstOrNull { it.id == selectedId }
                        ?: approvals.firstOrNull(),
                    errorMessage = null
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(
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
                    lastActionMessage = "Opened linked approval"
                )
                return@launch
            }

            runCatching {
                repository.getApproval(approvalId)
            }.onSuccess { selected ->
                _state.value = _state.value.copy(
                    loading = false,
                    selectedApproval = selected,
                    lastActionMessage = "Opened linked approval"
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

    fun decideNativeAction(actionId: String, allow: Boolean) {
        if (_state.value.decidingNativeActionId != null) return
        viewModelScope.launch {
            _state.value = _state.value.copy(
                decidingNativeActionId = actionId,
                errorMessage = null,
                lastActionMessage = null
            )
            val reason = if (allow) {
                "Approved this exact immutable native-agent action from Aixion Android."
            } else {
                "Denied this exact immutable native-agent action from Aixion Android."
            }
            runCatching {
                agentRunsRepository.decideExactAction(
                    actionId = actionId,
                    decision = if (allow) "ALLOW" else "BLOCK",
                    reason = reason
                )
            }.onSuccess {
                _state.value = _state.value.copy(
                    decidingNativeActionId = null,
                    lastActionMessage = if (allow) {
                        "Exact native-agent action approved"
                    } else {
                        "Exact native-agent action denied"
                    },
                    errorMessage = null
                )
                load(showLoading = false)
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    decidingNativeActionId = null,
                    errorMessage = error.message
                        ?: "Unable to record the exact native-agent decision."
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
