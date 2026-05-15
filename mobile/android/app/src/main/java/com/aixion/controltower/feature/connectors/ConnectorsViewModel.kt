package com.aixion.controltower.feature.connectors

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.BuildConfig
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
    val apiBaseUrl: String = BuildConfig.AIXION_API_BASE_URL,
    val lastSecret: String = "",
    val notice: String = "",
    val preview: String = "",
    val error: String = ""
) {
    val selectedTemplate: ConnectorTemplateDto?
        get() = templates.firstOrNull { it.id == selectedTemplateId } ?: templates.firstOrNull()

    fun webhookUrl(connector: ConnectorDto): String = "${apiBaseUrl.trimEnd('/')}/connectors/${connector.id}/webhook"

    fun setupText(connector: ConnectorDto, template: ConnectorTemplateDto?): String {
        val notes = template?.setup_notes?.joinToString(separator = "\n- ", prefix = "- ").orEmpty()
        return buildString {
            appendLine("Aixion connector setup")
            appendLine("Name: ${connector.name}")
            appendLine("Provider: ${connector.provider_label}")
            appendLine("Auth: ${connector.auth_type}")
            appendLine("Webhook: ${webhookUrl(connector)}")
            appendLine("Actions: ${connector.allowed_actions.joinToString()}")
            appendLine("Repositories: ${connector.allowed_repositories.joinToString().ifBlank { "wildcard" }}")
            if (template != null) {
                appendLine("Template: ${template.display_name}")
                appendLine("Mapper action: ${template.mapper.default_action}")
                appendLine("Mapped fields: ${template.mapper.field_paths.keys.joinToString()}")
            }
            if (notes.isNotBlank()) {
                appendLine("Setup notes:")
                appendLine(notes)
            }
        }.trim()
    }
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
        _state.value = _state.value.copy(selectedTemplateId = templateId, preview = "", notice = "Selected template updated")
    }

    fun markCopied(label: String) {
        _state.value = _state.value.copy(notice = "$label copied")
    }

    fun hideOneTimeSecret() {
        _state.value = _state.value.copy(lastSecret = "", notice = "One-time connector credential hidden")
    }

    fun createSelectedTemplateConnector() {
        val template = _state.value.selectedTemplate ?: return
        viewModelScope.launch {
            runCatching {
                repository.createFromTemplate(template)
                _state.value = _state.value.copy(notice = "Connector created from ${template.display_name}")
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
                _state.value = _state.value.copy(notice = "Connector ${if (connector.status == "ENABLED") "disabled" else "enabled"}")
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
                _state.value = _state.value.copy(
                    lastSecret = secret,
                    notice = if (connector.secret_configured) "Connector credential rotated. Copy it now." else "Connector credential issued. Copy it now."
                )
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
                _state.value = _state.value.copy(lastSecret = "", notice = "Connector credential revoked")
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
                _state.value = _state.value.copy(preview = "Mapper applied from ${template.display_name}", notice = "Mapper applied")
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
                val simulation = repository.simulateTemplatePayload(connector.id, template)
                _state.value = _state.value.copy(
                    preview = "accepted=${simulation.accepted}, auth=${simulation.auth_ready}, scope=${simulation.scope_ready}, action=${simulation.action}, errors=${simulation.errors}, warnings=${simulation.warnings}, normalized=${simulation.normalized_payload}",
                    notice = "Simulator result updated"
                )
            }.onFailure { error ->
                _state.value = _state.value.copy(error = error.message ?: "Failed to simulate connector payload")
            }
        }
    }
}
