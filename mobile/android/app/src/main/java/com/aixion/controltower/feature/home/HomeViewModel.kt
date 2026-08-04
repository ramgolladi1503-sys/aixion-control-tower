package com.aixion.controltower.feature.home

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.AgentRunsApiClient
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.TrustExceptionDto
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ProjectSummary
import com.aixion.controltower.core.model.isAwaitingGitHubExecution
import com.aixion.controltower.core.model.requiresHumanAction
import com.aixion.controltower.data.repository.AgentRunsRepository
import com.aixion.controltower.data.repository.ApprovalRepository
import com.aixion.controltower.data.repository.ProjectRepository
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class HomeUiState(
    val loading: Boolean = true,
    val projects: List<ProjectSummary> = emptyList(),
    val approvals: List<ApprovalSummary> = emptyList(),
    val trustExceptions: List<TrustExceptionDto> = emptyList(),
    val errorMessage: String? = null
) {
    val actionRequiredApprovals: List<ApprovalSummary> = approvals.filter { it.requiresHumanAction }
    val githubExecutionApprovals: List<ApprovalSummary> = approvals.filter { it.isAwaitingGitHubExecution }
    val readyForPrApprovals: List<ApprovalSummary> = approvals.filter { it.status == ApprovalStatus.READY_FOR_PR }
    val nativeActionApprovals: List<TrustExceptionDto> =
        trustExceptions.filter { it.isPendingExactAction }
    val nativeBlockedActions: List<TrustExceptionDto> =
        trustExceptions.filter { it.category == "BLOCK" && it.actionId != null }

    val pendingCount: Int = approvals.count { it.status == ApprovalStatus.PENDING_REVIEW }
    val blockedCount: Int =
        approvals.count { it.status == ApprovalStatus.BLOCKED } + nativeBlockedActions.size
    val actionRequiredCount: Int = actionRequiredApprovals.size + nativeActionApprovals.size
    val githubExecutionCount: Int = githubExecutionApprovals.size
    val failedTestsCount: Int = approvals.count { it.status == ApprovalStatus.TESTS_FAILED }
    val hasError: Boolean = errorMessage != null
}

class HomeViewModel(application: Application) : AndroidViewModel(application) {
    private val api = ApiClient.create(application.applicationContext)
    private val projectRepository = ProjectRepository(api)
    private val approvalRepository = ApprovalRepository(api)
    private val agentRunsRepository = AgentRunsRepository(
        AgentRunsApiClient.create(application.applicationContext)
    )
    private val refreshMutex = Mutex()

    private val _state = MutableStateFlow(HomeUiState())
    val state: StateFlow<HomeUiState> = _state.asStateFlow()

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
            runCatching {
                val projects = projectRepository.listProjects()
                val projectNames = projects.associate { it.id to it.name }
                val approvals = approvalRepository.listApprovals(projectNames)
                val trustExceptions = agentRunsRepository.listTrustExceptions()
                HomeUiState(
                    loading = false,
                    projects = projects,
                    approvals = approvals,
                    trustExceptions = trustExceptions
                )
            }.onSuccess { nextState ->
                _state.value = nextState
            }.onFailure { error ->
                _state.value = _state.value.copy(
                    loading = false,
                    errorMessage = "Backend sync failed. No mock data shown. ${error.message ?: "Retry when the backend is reachable."}"
                )
            }
        }
    }
}
