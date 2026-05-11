package com.aixion.controltower.feature.audit

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.model.AuditEventSummary
import com.aixion.controltower.data.repository.AuditRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class AuditTrailUiState(
    val loading: Boolean = true,
    val events: List<AuditEventSummary> = emptyList()
)

class AuditTrailViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AuditRepository(ApiClient.create(application.applicationContext))

    private val _state = MutableStateFlow(AuditTrailUiState())
    val state: StateFlow<AuditTrailUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = AuditTrailUiState(loading = false, events = repository.listAuditEvents())
        }
    }
}
