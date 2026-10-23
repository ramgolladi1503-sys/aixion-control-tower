package com.aixion.controltower.feature.home

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.model.isAwaitingGitHubExecution
import com.aixion.controltower.core.model.requiresHumanAction
import com.aixion.controltower.data.repository.ApprovalRepository
import com.aixion.controltower.data.repository.ProjectRepository
import java.time.Instant
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class HomeUiState(
    val loading: Boolean = true,
    val refreshing: Boolean = false,
    val projects: List<ProjectSummary> = emptyList(),
    val approvals: List<ApprovalSummary> = emptyList(),
    val lastSuccessfulSyncAt: Instant? = null,
    val errorMessage: String? = null
) {
    val actionRequiredApprovals: List<ApprovalSummary> = approvals.filter { it.requiresHumanAction }
    val githubExecutionApprovals: List<ApprovalSummary> = approvals.filter { it.isAwaitingGitHubExecution }
    val readyForPrApprovals: List<ApprovalSummary> = approvals.filter { it.status == ApprovalStatus.READY_FOR_PR }
    val unsupportedApprovals: List<ApprovalSummary> = approvals.filter {
        it.status == ApprovalStatus.UNKNOWN || it.risk == RiskLevel.UNKNOWN
    }

    val pendingCount: Int = approvals.count {
        it.status == ApprovalStatus.REQUESTED || it.status == ApprovalStatus.PENDING_REVIEW
    }
    val blockedCount: Int = approvals.count { it.status == ApprovalStatus.BLOCKED }
    val actionRequiredCount: Int = actionRequiredApprovals.size
    val githubExecutionCount: Int = githubExecutionApprovals.size
    val failedTestsCount: Int = approvals.count { it.status == ApprovalStatus.TESTS_FAILED }
    val hasError: Boolean = errorMessage != null
    val hasSuccessfulSync: Boolean = lastSuccessfulSyncAt != null
}

internal fun homeHeroTitle(state: HomeUiState): String = when {
    state.loading -> "Loading approvals"
    state.refreshing -> "Refreshing approvals"
    state.hasError -> "Approval service unavailable"
    state.unsupportedApprovals.isNotEmpty() -> "${state.unsupportedApprovals.size} approvals need an app update"
    state.actionRequiredCount > 0 -> "${state.actionRequiredCount} requests require review"
    state.blockedCount > 0 -> "${state.blockedCount} blocked actions need attention"
    state.hasSuccessfulSync -> "No approvals waiting"
    else -> "Status not verified"
}

internal fun homeHeroStatusLabel(state: HomeUiState): String = when {
    state.hasError -> "Service unavailable"
    state.loading || state.refreshing -> "Syncing"
    !state.hasSuccessfulSync && !state.loading && !state.refreshing -> "Not verified"
    state.unsupportedApprovals.isNotEmpty() -> "Unsupported approval"
    state.actionRequiredCount > 0 -> "Approval queue"
    else -> "Backend synced"
}

internal fun shouldShowHomeStatusHero(state: HomeUiState): Boolean = !state.hasError

internal fun homeHeroSubtitle(state: HomeUiState): String = when {
    state.loading -> "Syncing approval requests."
    state.refreshing -> "Checking for current approval requests."
    state.hasError -> "We couldn't load your approvals. Check your connection, then retry."
    state.unsupportedApprovals.isNotEmpty() -> "The app cannot safely interpret some approval status or risk values. No decisions are enabled for them."
    state.actionRequiredCount > 0 -> "AI/code execution is paused until you approve, deny, or request revision."
    state.blockedCount > 0 -> "Policy stopped unsafe work. Review the blocked queue before retrying."
    state.hasSuccessfulSync -> "A fresh backend sync found no requests awaiting your decision. Agent connection status is not available here."
    else -> "A successful backend sync is required before approval status can be shown."
}

internal fun homeBackendStatusLabel(
    loading: Boolean,
    refreshing: Boolean,
    hasError: Boolean,
    hasSuccessfulSync: Boolean
): String = when {
    loading || refreshing -> "Syncing"
    hasError -> "Sync error"
    hasSuccessfulSync -> "Synced"
    else -> "Not verified"
}

internal fun homeRefreshFailureState(state: HomeUiState, message: String): HomeUiState = state.copy(
    loading = false,
    refreshing = false,
    errorMessage = message
)

class HomeViewModel(application: Application) : AndroidViewModel(application) {
    private val api = ApiClient.create(application.applicationContext)
    private val projectRepository = ProjectRepository(api)
    private val approvalRepository = ApprovalRepository(api)

    private val _state = MutableStateFlow(HomeUiState())
    val state: StateFlow<HomeUiState> = _state.asStateFlow()
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
        _state.value = _state.value.copy(
            loading = showLoading,
            refreshing = true,
            errorMessage = null
        )
        try {
            val projects = projectRepository.listProjects()
            val projectNames = projects.associate { it.id to it.name }
            val approvals = approvalRepository.listApprovals(projectNames)
            _state.value = HomeUiState(
                loading = false,
                refreshing = false,
                projects = projects,
                approvals = approvals,
                lastSuccessfulSyncAt = Instant.now()
            )
            return true
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            _state.value = homeRefreshFailureState(
                _state.value,
                "Backend sync failed. No mock data shown. ${error.message ?: "Retry when the backend is reachable."}"
            )
            return false
        } finally {
            refreshInFlight = false
        }
    }
}
