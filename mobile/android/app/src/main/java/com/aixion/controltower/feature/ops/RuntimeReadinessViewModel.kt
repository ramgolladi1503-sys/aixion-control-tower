package com.aixion.controltower.feature.ops

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.OpsApiClient
import com.aixion.controltower.core.model.RuntimeReadinessSummary
import com.aixion.controltower.data.repository.OpsRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class RuntimeReadinessUiState(
    val loading: Boolean = true,
    val readiness: RuntimeReadinessSummary? = null,
    val errorMessage: String = ""
)

class RuntimeReadinessViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = OpsRepository(OpsApiClient.create(application.applicationContext))

    private val _state = MutableStateFlow(RuntimeReadinessUiState())
    val state: StateFlow<RuntimeReadinessUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, errorMessage = "")
            try {
                _state.value = RuntimeReadinessUiState(
                    loading = false,
                    readiness = repository.getRuntimeReadiness()
                )
            } catch (error: Exception) {
                _state.value = RuntimeReadinessUiState(
                    loading = false,
                    errorMessage = error.message ?: "Unable to load runtime readiness"
                )
            }
        }
    }
}
