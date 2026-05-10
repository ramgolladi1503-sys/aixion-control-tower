package com.aixion.controltower.feature.home

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.ProjectSummary
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
    val approvals: List<ApprovalSummary> = emptyList()
) {
    val actionRequiredApprovals: List<ApprovalSummary> = approvals.filter { it.requiresHumanAction }
    val githubExecutionApprovals: List<ApprovalSummary> = approvals.filter { it.isAwaitingGitHubExecution }
    val readyForPrApprovals: List<ApprovalSummary> = approvals.filter { it.status == ApprovalStatus.READY_FOR_PR }

    val pendingCount: Int = approvals.count { it.status == ApprovalStatus.PENDING_REVIEW }
    val blockedCount: Int = approvals.count { it.status == ApprovalStatus.BLOCKED }
    val actionRequiredCount: Int = actionRequiredApprovals.size
    val githubExecutionCount: Int = githubExecutionApprovals.size
    val failedTestsCount: Int = approvals.count { it.status == ApprovalStatus.TESTS_FAILED }
}

class HomeViewModel(application: Application) : AndroidViewModel(application) {
    private val api = ApiClient.create(application.applicationContext)
    private val projectRepository = ProjectRepository(api)
    private val approvalRepository = ApprovalRepository(api)

    private val _state = MutableStateFlow(HomeUiState())
    val state: StateFlow<HomeUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            val projects = projectRepository.listProjects()
            val projectNames = projects.associate { it.id to it.name }
            val approvals = approvalRepository.listApprovals(projectNames)
            _state.value = HomeUiState(loading = false, projects = projects, approvals = approvals)
        }
    }
}
