package com.aixion.controltower.feature.approvals

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.data.repository.ApprovalRepository
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

internal fun decisionMatchesStatus(decision: String?, status: ApprovalStatus): Boolean {
    val expected = when (decision?.lowercase()) {
        "approve" -> ApprovalStatus.APPROVED
        "reject" -> ApprovalStatus.DENIED
        "revise" -> ApprovalStatus.REVISION_REQUESTED
        else -> null
    }
    return expected != null && status == expected
}

internal fun decisionOutcomeUnknown(
    current: ApprovalsUiState,
    approvalId: String,
    decision: String?,
    message: String
): ApprovalsUiState = current.copy(
    decisionInFlight = false,
    decisionNeedsReconciliation = true,
    decisionBeingReconciled = decision,
    lastActionMessage = message,
    lastActionApprovalId = approvalId
)

internal fun canStartApprovalDecision(state: ApprovalsUiState): Boolean =
    !state.decisionInFlight && !state.decisionNeedsReconciliation

internal fun retainDecisionInHistory(
    history: List<ApprovalSummary>,
    updated: ApprovalSummary
): List<ApprovalSummary> = if (history.any { it.id == updated.id }) {
    history.map { if (it.id == updated.id) updated else it }
} else {
    history + updated
}

data class ApprovalsUiState(
    val loading: Boolean = true,
    val approvals: List<ApprovalSummary> = emptyList(),
    val selectedApproval: ApprovalSummary? = null,
    val openingApprovalId: String? = null,
    val detailTargetApprovalId: String? = null,
    val lastActionMessage: String? = null,
    val lastActionApprovalId: String? = null,
    val decisionInFlight: Boolean = false,
    val decisionNeedsReconciliation: Boolean = false,
    val decisionBeingReconciled: String? = null,
    val errorMessage: String? = null
) {
    val hasError: Boolean = errorMessage != null
}

internal fun reconcileApprovalsAfterRefresh(
    current: ApprovalsUiState,
    refreshedApprovals: List<ApprovalSummary>
): ApprovalsUiState {
    val targetId = current.openingApprovalId
        ?: current.detailTargetApprovalId
        ?: current.selectedApproval?.id
    val exactMatch = targetId?.let { id -> refreshedApprovals.firstOrNull { it.id == id } }
    val selected = when {
        targetId == null -> refreshedApprovals.firstOrNull()
        exactMatch != null -> exactMatch
        current.openingApprovalId != null -> null
        else -> current.selectedApproval?.takeIf { it.id == targetId }
    }
    val recoveredDeepLinkFailure = exactMatch != null &&
        (current.openingApprovalId != null || current.lastActionMessage?.startsWith("Failed to open approval") == true)

    return current.copy(
        loading = false,
        approvals = refreshedApprovals,
        selectedApproval = selected,
        openingApprovalId = current.openingApprovalId?.takeIf { selected == null },
        lastActionMessage = if (recoveredDeepLinkFailure) {
            "Opened approval from notification or linked work."
        } else {
            current.lastActionMessage
        },
        lastActionApprovalId = if (recoveredDeepLinkFailure) targetId else current.lastActionApprovalId,
        errorMessage = null
    )
}

class ApprovalsViewModel internal constructor(
    application: Application,
    private val repository: ApprovalRepository
) : AndroidViewModel(application) {
    constructor(application: Application) : this(
        application,
        ApprovalRepository(ApiClient.create(application.applicationContext))
    )

    private val decisionSubmissionLock = Any()

    private val _state = MutableStateFlow(ApprovalsUiState())
    val state: StateFlow<ApprovalsUiState> = _state.asStateFlow()
    private var openApprovalRequestSequence = 0L
    private var refreshInFlight = false

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch { refresh(showLoading = true) }
    }

    suspend fun refreshInForeground(): Boolean? = refresh(showLoading = false)

    private suspend fun refresh(showLoading: Boolean): Boolean? {
        if (refreshInFlight) return null
        refreshInFlight = true
        if (showLoading) {
            _state.value = _state.value.copy(loading = true, errorMessage = null)
        }
        try {
            val approvals = repository.listApprovals()
            _state.value = reconcileApprovalsAfterRefresh(_state.value, approvals)
            return true
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            _state.value = _state.value.copy(
                loading = false,
                errorMessage = "Backend approval sync failed. No mock approvals shown. ${error.message ?: "Retry when the backend is reachable."}"
            )
            return false
        } finally {
            refreshInFlight = false
        }
    }

    fun selectApproval(approval: ApprovalSummary) {
        if (_state.value.decisionInFlight || _state.value.decisionNeedsReconciliation) return
        openApprovalRequestSequence += 1
        _state.value = _state.value.copy(
            selectedApproval = approval,
            openingApprovalId = null,
            detailTargetApprovalId = null,
            lastActionMessage = null,
            lastActionApprovalId = null
        )
    }

    fun openApprovalById(approvalId: String) {
        if (_state.value.decisionInFlight || _state.value.decisionNeedsReconciliation) return
        openApprovalRequestSequence += 1
        val requestSequence = openApprovalRequestSequence
        if (approvalId.isBlank() || approvalId.length > 128 || approvalId.any(Char::isWhitespace)) {
            _state.value = _state.value.copy(
                selectedApproval = null,
                openingApprovalId = null,
                detailTargetApprovalId = null,
                loading = false,
                lastActionMessage = "Failed to open approval: invalid approval identifier.",
                lastActionApprovalId = approvalId
            )
            return
        }

        _state.value = _state.value.copy(
            selectedApproval = null,
            openingApprovalId = approvalId,
            detailTargetApprovalId = approvalId,
            loading = true,
            errorMessage = null,
            lastActionMessage = null,
            lastActionApprovalId = approvalId
        )
        viewModelScope.launch {
            val cachedApproval = _state.value.approvals.firstOrNull { it.id == approvalId }
            if (cachedApproval != null) {
                if (requestSequence != openApprovalRequestSequence) return@launch
                _state.value = _state.value.copy(
                    loading = false,
                    selectedApproval = cachedApproval,
                    openingApprovalId = null,
                    detailTargetApprovalId = approvalId,
                    lastActionMessage = "Opened approval from notification or linked work.",
                    lastActionApprovalId = approvalId
                )
                return@launch
            }

            val selected = try {
                repository.getApproval(approvalId)
            } catch (cancelled: kotlinx.coroutines.CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                if (requestSequence != openApprovalRequestSequence) return@launch
                val current = _state.value
                if (current.selectedApproval?.id == approvalId && current.openingApprovalId == null) {
                    return@launch
                }
                _state.value = _state.value.copy(
                    loading = false,
                    selectedApproval = null,
                    openingApprovalId = null,
                    detailTargetApprovalId = approvalId,
                    lastActionMessage = "Failed to open approval $approvalId: ${error.message ?: "backend request failed"}",
                    lastActionApprovalId = approvalId
                )
                return@launch
            }
            if (requestSequence != openApprovalRequestSequence) return@launch
            _state.value = _state.value.copy(
                loading = false,
                selectedApproval = selected,
                openingApprovalId = null,
                detailTargetApprovalId = approvalId,
                lastActionMessage = "Opened approval from notification or linked work.",
                lastActionApprovalId = approvalId
            )
        }
    }

    fun decide(decision: String, reason: String, onCompleted: () -> Unit = {}) {
        val approval = synchronized(decisionSubmissionLock) {
            val current = _state.value
            if (!canStartApprovalDecision(current)) return
            val selected = current.selectedApproval ?: return
            if (selected.status != ApprovalStatus.REQUESTED &&
                selected.status != ApprovalStatus.PENDING_REVIEW
            ) return
            _state.value = current.copy(
                decisionInFlight = true,
                decisionBeingReconciled = null,
                lastActionMessage = "Sending $decision decision...",
                lastActionApprovalId = selected.id
            )
            selected
        }
        viewModelScope.launch {
            val latest = try {
                repository.getApproval(approval.id)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (error: Exception) {
                _state.value = _state.value.copy(
                    decisionInFlight = false,
                    lastActionMessage = "Approval could not be refreshed before the decision. No decision was sent. Refresh and review the request before trying again. ${error.message ?: ""}".trim(),
                    lastActionApprovalId = approval.id
                )
                return@launch
            }
            if (latest != approval) {
                _state.value = _state.value.copy(
                    decisionInFlight = false,
                    decisionNeedsReconciliation = false,
                    decisionBeingReconciled = null,
                    selectedApproval = latest,
                    approvals = retainDecisionInHistory(_state.value.approvals, latest),
                    lastActionMessage = "This approval changed after it was opened. No decision was sent. Review the current request before deciding.",
                    lastActionApprovalId = latest.id
                )
                return@launch
            }
            if (latest.status != ApprovalStatus.REQUESTED &&
                latest.status != ApprovalStatus.PENDING_REVIEW
            ) {
                _state.value = _state.value.copy(
                    decisionInFlight = false,
                    decisionNeedsReconciliation = false,
                    decisionBeingReconciled = null,
                    selectedApproval = latest,
                    approvals = retainDecisionInHistory(_state.value.approvals, latest),
                    lastActionMessage = "This approval is now ${latest.status.name}. No decision was sent.",
                    lastActionApprovalId = latest.id
                )
                return@launch
            }
            val updated = try {
                repository.decide(approval.id, decision, reason).also { result ->
                    if (!decisionMatchesStatus(decision, result.status)) {
                        throw IllegalStateException(
                            "Server returned ${result.status.name} for the requested $decision decision."
                        )
                    }
                }
            } catch (cancelled: CancellationException) {
                _state.value = decisionOutcomeUnknown(
                    _state.value,
                    approval.id,
                    decision,
                    "Decision request was interrupted. The server result is unknown. Check this approval’s status before retrying."
                )
                throw cancelled
            } catch (error: Exception) {
                reconcileDecisionAfterFailure(approval, decision, error, onCompleted)
                return@launch
            }
            completeDecision(approval, updated, decision, onCompleted)
        }
    }

    private suspend fun reconcileDecisionAfterFailure(
        original: ApprovalSummary,
        decision: String,
        error: Exception,
        onCompleted: () -> Unit
    ) {
        val latest = try {
            repository.getApproval(original.id)
        } catch (cancelled: CancellationException) {
            _state.value = decisionOutcomeUnknown(
                _state.value,
                original.id,
                decision,
                "Decision status check was interrupted. The server result remains unknown; check again before retrying."
            )
            throw cancelled
        } catch (_: Exception) {
            _state.value = decisionOutcomeUnknown(
                _state.value,
                original.id,
                decision,
                "Decision result is unknown. The server could not confirm whether it committed. Check this approval’s status before retrying. ${error.message ?: ""}".trim()
            )
            return
        }

        if (latest.status == com.aixion.controltower.core.model.ApprovalStatus.REQUESTED ||
            latest.status == com.aixion.controltower.core.model.ApprovalStatus.PENDING_REVIEW
        ) {
            _state.value = _state.value.copy(
                decisionInFlight = false,
                decisionNeedsReconciliation = false,
                decisionBeingReconciled = null,
                selectedApproval = latest,
                approvals = _state.value.approvals.map { if (it.id == latest.id) latest else it },
                lastActionMessage = "Decision was not confirmed. The server still shows this approval as pending. Review its current details before retrying. ${error.message ?: ""}".trim(),
                lastActionApprovalId = latest.id
            )
            return
        }

        if (decisionMatchesStatus(decision, latest.status)) {
            completeDecision(original, latest, decision, onCompleted, reconciled = true)
            return
        }

        _state.value = _state.value.copy(
            decisionInFlight = false,
            decisionNeedsReconciliation = false,
            decisionBeingReconciled = null,
            selectedApproval = latest,
            approvals = _state.value.approvals.map { if (it.id == latest.id) latest else it },
            lastActionMessage = "This approval is now ${latest.status.name}; the requested $decision decision was not confirmed. No retry was sent.",
            lastActionApprovalId = latest.id
        )
    }

    fun reconcileDecisionStatus() {
        val approvalId = _state.value.selectedApproval?.id ?: return
        if (!_state.value.decisionNeedsReconciliation || _state.value.decisionInFlight) return
        viewModelScope.launch {
            _state.value = _state.value.copy(decisionInFlight = true)
            try {
                val latest = repository.getApproval(approvalId)
                val decision = _state.value.decisionBeingReconciled
                if (decision != null && decisionMatchesStatus(decision, latest.status)) {
                    completeDecision(_state.value.selectedApproval ?: latest, latest, decision, {})
                } else {
                    _state.value = _state.value.copy(
                        decisionInFlight = false,
                        decisionNeedsReconciliation = false,
                        decisionBeingReconciled = null,
                        selectedApproval = latest,
                        approvals = _state.value.approvals.map { if (it.id == latest.id) latest else it },
                        lastActionMessage = if (latest.status == com.aixion.controltower.core.model.ApprovalStatus.REQUESTED || latest.status == com.aixion.controltower.core.model.ApprovalStatus.PENDING_REVIEW) {
                            "Server confirms this approval is still pending. Review its current details before retrying."
                        } else {
                            "Server reports status ${latest.status.name}. No decision retry was sent."
                        },
                        lastActionApprovalId = latest.id
                    )
                }
            } catch (cancelled: CancellationException) {
                _state.value = decisionOutcomeUnknown(
                    _state.value,
                    approvalId,
                    _state.value.decisionBeingReconciled,
                    "Decision status check was interrupted. The result remains unknown; check again before retrying."
                )
                throw cancelled
            } catch (error: Exception) {
                _state.value = _state.value.copy(
                    decisionInFlight = false,
                    decisionNeedsReconciliation = true,
                    lastActionMessage = "Decision result is still unknown. Status check failed: ${error.message ?: "backend request failed"}",
                    lastActionApprovalId = approvalId
                )
            }
        }
    }

    private suspend fun completeDecision(
        original: ApprovalSummary,
        updated: ApprovalSummary,
        decision: String,
        onCompleted: () -> Unit,
        reconciled: Boolean = false
    ) {
        val resolveMessage = if (original.isMCPToolApproval && updated.status == com.aixion.controltower.core.model.ApprovalStatus.APPROVED) {
            try {
                val result = repository.resolveMCPApproval(original.id)
                if (result.forwarded) " MCP request forwarded." else " MCP resolve: ${result.reason}"
            } catch (cancelled: CancellationException) {
                " MCP forwarding was interrupted and remains unconfirmed."
            } catch (error: Exception) {
                " MCP forwarding remains unconfirmed: ${error.message ?: "backend request failed"}."
            }
        } else {
            ""
        }
        val history = retainDecisionInHistory(_state.value.approvals, updated)
        val otherApprovals = history.filterNot { it.id == updated.id }
        val nextSelection = otherApprovals.firstOrNull {
            it.status == com.aixion.controltower.core.model.ApprovalStatus.REQUESTED ||
                it.status == com.aixion.controltower.core.model.ApprovalStatus.PENDING_REVIEW
        } ?: otherApprovals.firstOrNull() ?: updated
        _state.value = _state.value.copy(
            decisionInFlight = false,
            decisionNeedsReconciliation = false,
            decisionBeingReconciled = null,
            approvals = history,
            selectedApproval = nextSelection,
            openingApprovalId = null,
            detailTargetApprovalId = null,
            lastActionMessage = if (reconciled) {
                "Server confirms the $decision decision was recorded. The response was lost, so status was reconciled.$resolveMessage"
            } else {
                "Decision sent: $decision. Request moved out of the active queue.$resolveMessage"
            },
            lastActionApprovalId = updated.id
        )
        onCompleted()
    }
}
