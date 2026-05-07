package com.aixion.controltower.feature.tests

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.TestRunSummary
import com.aixion.controltower.data.repository.TestRunRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class TestRunsUiState(
    val loading: Boolean = true,
    val testRuns: List<TestRunSummary> = emptyList()
)

class TestRunsViewModel : ViewModel() {
    private val repository = TestRunRepository(ApiClient.create())

    private val _state = MutableStateFlow(TestRunsUiState())
    val state: StateFlow<TestRunsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = TestRunsUiState(loading = false, testRuns = repository.listTestRuns())
        }
    }
}
