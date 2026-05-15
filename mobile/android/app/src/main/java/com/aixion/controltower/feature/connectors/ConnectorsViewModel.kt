package com.aixion.controltower.feature.connectors

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import com.aixion.controltower.data.repository.ConnectorRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class ConnectorsUiState(
    val loading: Boolean = true,
    val connectors: List<ConnectorDto> = emptyList(),
    val templates: List<ConnectorTemplateDto> = emptyList(),
    val selectedTemplateId: String? = null,
    val lastSecret: String = "",
    val preview: String = "",
    val error: String = ""
) {
    val selectedTemplate: ConnectorTemplateDto?
        get() = templates.firstOrNull { it.id == selectedTemplateId } ?: templates.firstOrNull()
}

class ConnectorsViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = ConnectorRepository(ApiClient.create(application.applicationContext))
    private val _state = MutableStateFlow(ConnectorsUiState())
    val state: StateFlow<ConnectorsUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            runCatching {
                val connectors = repository.listConnectors()
                val templates = repository.listTemplates()
                _state.value = _state.value.copy(
                    loading = false,
                    connectors = connectors,
                    templates = templates,
                    selectedTemplateId = _state.value.selectedTemplateId ?: templates.firstOrNull()?.id,
                    error = ""
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(loading = false, error = error.message ?: "Failed to load connectors")
            }
        }
    }

    fun selectTemplate(templateId: String) {
        _state.value = _state.value.copy(selectedTemplateId = templateId, preview = "")
    }

    fun createSelectedTemplateConnector() {
        val template = _state.value.selectedTemplate ?: return
        viewModelScope.launch {
            runCatching {
                repository.createFromTemplate(template)
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to create connector")
            }
        }
    }

    fun toggle(connector: ConnectorDto) {
        viewModelScope.launch {
            runCatching {
                if (connector.status == "ENABLED") repository.disable(connector.id) else repository.enable(connector.id)
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to update connector")
            }
        }
    }

    fun issueSecret(connector: ConnectorDto) {
        viewModelScope.launch {
            runCatching {
                val secret = if (connector.secret_configured) repository.rotateSecret(connector.id) else repository.issueSecret(connector.id)
                _state.value = _state.value.copy(lastSecret = secret)
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to issue connector secret")
            }
        }
    }

    fun revokeSecret(connector: ConnectorDto) {
        viewModelScope.launch {
            runCatching {
                repository.revokeSecret(connector.id)
                _state.value = _state.value.copy(lastSecret = "")
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to revoke connector secret")
            }
        }
    }

    fun applyTemplateMapper(connector: ConnectorDto) {
        val template = _state.value.selectedTemplate ?: return
        viewModelScope.launch {
            runCatching {
                repository.applyTemplateMapper(connector.id, template.mapper)
                _state.value = _state.value.copy(preview = "Mapper applied from ${template.display_name}")
                refresh()
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to apply mapper")
            }
        }
    }

    fun previewTemplateMapper(connector: ConnectorDto) {
        val template = _state.value.selectedTemplate ?: return
        viewModelScope.launch {
            runCatching {
                val preview = repository.previewMapper(connector.id, template)
                _state.value = _state.value.copy(
                    preview = "mapper=${preview.mapper_enabled}, normalized=${preview.normalized_payload}, warnings=${preview.warnings}"
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to preview mapper")
            }
        }
    }
}
