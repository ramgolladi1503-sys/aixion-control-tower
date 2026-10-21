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
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class HomeUiState(
    val loading: Boolean = true,
    val projects: List<ProjectSummary> = emptyList(),
    val approvals: List<ApprovalSummary> = emptyList(),
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
}

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
        if (showLoading) {
            _state.value = _state.value.copy(loading = true, errorMessage = null)
        }
        try {
            val projects = projectRepository.listProjects()
            val projectNames = projects.associate { it.id to it.name }
            val approvals = approvalRepository.listApprovals(projectNames)
            _state.value = HomeUiState(loading = false, projects = projects, approvals = approvals)
            return true
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (error: Exception) {
            _state.value = HomeUiState(
                loading = false,
                errorMessage = "Backend sync failed. No mock data shown. ${error.message ?: "Retry when the backend is reachable."}"
            )
            return false
        } finally {
            refreshInFlight = false
        }
    }
}
