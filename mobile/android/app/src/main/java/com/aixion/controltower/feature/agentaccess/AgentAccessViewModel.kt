package com.aixion.controltower.feature.agentaccess

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.core.api.dto.AgentRegistrationResponseDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.LOCAL_APPROVAL_CONNECTOR_PROVIDERS
import com.aixion.controltower.core.api.dto.isExpectedApprovalConnectorAgent
import com.aixion.controltower.data.repository.AgentAccessRepository
import com.aixion.controltower.data.repository.AgentAccessSummary
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.util.Locale

data class AgentAccessUiState(
    val loading: Boolean = true,
    val working: Boolean = false,
    val agents: List<AgentAccessSummary> = emptyList(),
    val projects: List<ProjectDto> = emptyList(),
    val projectsLoaded: Boolean = false,
    val projectCreationNeedsRefresh: Boolean = false,
    val newProjectName: String = "",
    val selectedProvider: String = "ANTIGRAVITY",
    val selectedProjectId: String? = null,
    val displayName: String = "Antigravity on this Mac",
    val oneTimeToken: String? = null,
    val oneTimeProvider: String? = null,
    val oneTimeAgentName: String? = null,
    val oneTimeAgentId: String? = null,
    val oneTimeProjectId: String? = null,
    val notice: String = "",
    val error: String = ""
)

internal fun canCreateApprovalAgent(state: AgentAccessUiState): Boolean =
    !state.loading &&
        !state.working &&
        state.error.isBlank() &&
        state.projectsLoaded &&
        !state.projectCreationNeedsRefresh &&
        !state.selectedProjectId.isNullOrBlank() &&
        state.projects.any { it.id.isNotBlank() && it.id == state.selectedProjectId }

internal fun agentAccessUiStateForProvider(provider: String?): AgentAccessUiState {
    val selectedProvider = when (provider?.trim()?.uppercase(Locale.ROOT)) {
        "CODEX" -> "CODEX"
        else -> "ANTIGRAVITY"
    }
    return AgentAccessUiState(
        selectedProvider = selectedProvider,
        displayName = if (selectedProvider == "CODEX") "Codex on this Mac" else "Antigravity on this Mac"
    )
}

class AgentAccessViewModel internal constructor(
    application: Application,
    savedStateHandle: SavedStateHandle,
    private val repository: AgentAccessRepository
) : AndroidViewModel(application) {
    constructor(application: Application, savedStateHandle: SavedStateHandle) : this(
        application,
        savedStateHandle,
        AgentAccessRepository(ApiClient.create(application.applicationContext))
    )

    private val _state = MutableStateFlow(agentAccessUiStateForProvider(savedStateHandle["provider"]))
    val state: StateFlow<AgentAccessUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = "")
            try {
                val provider = _state.value.selectedProvider
                val projects = repository.listProjects()
                val agents = repository.listApprovalAgents(provider)
                val current = _state.value
                _state.value = current.copy(
                    loading = false,
                    projects = projects,
                    projectsLoaded = true,
                    projectCreationNeedsRefresh = false,
                    agents = agents,
                    selectedProjectId = current.selectedProjectId
                        ?.takeIf { id -> projects.any { it.id == id } }
                        ?: projects.firstOrNull()?.id,
                    error = ""
                )
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                _state.value = _state.value.copy(
                    loading = false,
                    projectsLoaded = false,
                    error = "Could not load projects or ${providerDisplayName(_state.value.selectedProvider)} agents. Check owner access and connection, then retry."
                )
            }
        }
    }

    fun setDisplayName(value: String) {
        _state.value = _state.value.copy(displayName = value.take(100), error = "")
    }

    fun selectProvider(provider: String) {
        val current = _state.value
        if (current.working || current.loading || provider !in LOCAL_APPROVAL_CONNECTOR_PROVIDERS) return
        if (provider == current.selectedProvider) return
        val defaults = mapOf(
            "ANTIGRAVITY" to "Antigravity on this Mac",
            "CODEX" to "Codex on this Mac"
        )
        val nextName = if (current.displayName == defaults[current.selectedProvider]) {
            defaults[provider].orEmpty()
        } else {
            current.displayName
        }
        _state.value = current.copy(
            selectedProvider = provider,
            displayName = nextName,
            agents = emptyList(),
            loading = true,
            error = "",
            notice = ""
        )
        refresh()
    }

    fun selectProject(projectId: String) {
        if (_state.value.projects.any { it.id == projectId }) {
            _state.value = _state.value.copy(selectedProjectId = projectId, error = "")
        }
    }

    fun setNewProjectName(value: String) {
        if (_state.value.working) return
        _state.value = _state.value.copy(newProjectName = value, error = "")
    }

    fun createProject() {
        val current = _state.value
        val name = current.newProjectName.trim()
        if (current.loading || current.working || !current.projectsLoaded ||
            current.projectCreationNeedsRefresh || current.error.isNotBlank()
        ) return
        if (current.projects.isNotEmpty()) {
            _state.value = current.copy(
                error = "A project is already available. Select it or refresh before creating another."
            )
            return
        }
        if (name.isBlank()) {
            _state.value = current.copy(error = "Enter a project name to continue.")
            return
        }

        viewModelScope.launch {
            _state.value = _state.value.copy(working = true, error = "", notice = "")
            try {
                val project = repository.createProject(name)
                val latest = _state.value
                _state.value = latest.copy(
                    working = false,
                    projects = (latest.projects.filterNot { it.id == project.id } + project),
                    selectedProjectId = project.id,
                    newProjectName = "",
                    projectCreationNeedsRefresh = false,
                    notice = "Project created in your account workspace using Strict mode. You can now create a project-limited connector.",
                    error = ""
                )
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                _state.value = _state.value.copy(
                    working = false,
                    projectCreationNeedsRefresh = true,
                    error = "Project creation was not confirmed. Refresh the project list before retrying."
                )
            }
        }
    }

    fun createAgent() {
        val current = _state.value
        val name = current.displayName.trim()
        val projectId = current.selectedProjectId
        val provider = current.selectedProvider
        if (name.isBlank() || projectId == null || current.working || !canCreateApprovalAgent(current)) {
            _state.value = current.copy(error = "Enter an agent name and select one project.")
            return
        }
        runCredentialAction(
            "${providerDisplayName(provider)} connector created",
            expectedProvider = provider,
            expectedProjectId = projectId
        ) {
            repository.createApprovalAgent(name, projectId, provider)
        }
    }

    fun rotateToken(agentId: String) {
        if (_state.value.working) return
        val agent = _state.value.agents.firstOrNull { it.agent.id == agentId }?.agent
        val projectId = agent?.allowed_project_ids?.singleOrNull()
        if (projectId == null) {
            _state.value = _state.value.copy(error = "Agent scope is unknown. Refresh before rotating its credential.")
            return
        }
        runCredentialAction(
            "${providerDisplayName(agent.provider)} token rotated",
            expectedProvider = agent.provider,
            expectedProjectId = projectId,
            expectedAgentId = agentId
        ) {
            repository.rotateToken(agentId)
        }
    }

    fun revokeToken(agentId: String) {
        val current = _state.value
        if (current.working) return
        val selectedAgent = current.agents.firstOrNull { it.agent.id == agentId }?.agent
        if (selectedAgent == null || selectedAgent.provider != current.selectedProvider) {
            _state.value = current.copy(error = "Connector is not in the selected platform list. Refresh before revoking.")
            return
        }
        val provider = selectedAgent.provider
        viewModelScope.launch {
            _state.value = _state.value.copy(working = true, error = "", notice = "")
            try {
                val revoked = repository.revokeToken(agentId)
                if (revoked.id != agentId) {
                    _state.value = _state.value.copy(
                        working = false,
                        agents = _state.value.agents.map { summary ->
                            if (summary.agent.id == agentId) summary.copy(credential = summary.credential?.copy(credential_state = "UNKNOWN", token_present = false)) else summary
                        },
                        error = "Server returned a different agent after revocation. Refresh and verify credential state."
                    )
                    return@launch
                }
                val revokedCredential = AgentCredentialStatusDto(
                    agent_id = agentId, auth_type = revoked.auth_type, credential_state = "REVOKED", token_present = false
                )
                _state.value = _state.value.copy(
                    working = false,
                    agents = _state.value.agents.map { summary -> if (summary.agent.id == agentId) summary.copy(agent = revoked, credential = revokedCredential) else summary },
                    oneTimeToken = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeToken,
                    oneTimeProvider = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeProvider,
                    oneTimeAgentName = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeAgentName,
                    oneTimeAgentId = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeAgentId,
                    oneTimeProjectId = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeProjectId,
                    notice = "Agent token revoked. The Mac hook will now fail closed."
                )
                try {
                    _state.value = _state.value.copy(agents = repository.listApprovalAgents(provider))
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    _state.value = _state.value.copy(error = "Token revoked, but the agent list could not refresh. The local row shows REVOKED; verify after reconnecting.")
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                _state.value = _state.value.copy(
                    working = false,
                    error = "Token revocation failed. The existing credential state was not confirmed."
                )
            }
        }
    }

    fun clearOneTimeToken() {
        _state.value = _state.value.copy(
            oneTimeToken = null,
            oneTimeProvider = null,
            oneTimeAgentName = null,
            oneTimeAgentId = null,
            oneTimeProjectId = null
        )
    }

    private fun runCredentialAction(
        successMessage: String,
        expectedProvider: String,
        expectedProjectId: String,
        expectedAgentId: String? = null,
        action: suspend () -> AgentRegistrationResponseDto
    ) {
        viewModelScope.launch {
            _state.value = _state.value.copy(working = true, error = "", notice = "")
            try {
                val response = action()
                val token = response.agent_token
                val agent = response.agent
                if (token.isNullOrBlank() ||
                    !isExpectedApprovalConnectorAgent(agent, expectedProjectId, expectedProvider) ||
                    (expectedAgentId != null && agent.id != expectedAgentId)
                ) {
                    _state.value = _state.value.copy(
                        working = false,
                        error = "Server response did not confirm the exact ${providerDisplayName(expectedProvider)} project, actions, and credential. Do not install this token; refresh and reconcile agent state."
                    )
                    return@launch
                }
                val credential = AgentCredentialStatusDto(
                    agent_id = agent.id, auth_type = agent.auth_type, credential_state = "ACTIVE", token_present = true
                )
                _state.value = _state.value.copy(
                    working = false,
                    agents = (_state.value.agents.filterNot { it.agent.id == agent.id } + AgentAccessSummary(agent, credential)),
                    oneTimeToken = token,
                    oneTimeProvider = expectedProvider,
                    oneTimeAgentName = agent.display_name,
                    oneTimeAgentId = agent.id,
                    oneTimeProjectId = expectedProjectId,
                    notice = "$successMessage. Copy the token into macOS Keychain now; it will not be shown again.",
                    error = ""
                )
                try {
                    val agents = repository.listApprovalAgents(expectedProvider)
                    _state.value = _state.value.copy(agents = agents)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    _state.value = _state.value.copy(
                        error = "Credential issued and shown above, but the agent list could not refresh. Save the token before leaving this screen."
                    )
                }
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                _state.value = _state.value.copy(
                    working = false,
                    error = "The request failed. Refresh the agent list before retrying to avoid creating duplicate credentials."
                )
            }
        }
    }
}

private fun providerDisplayName(provider: String): String = when (provider) {
    "CODEX" -> "Codex"
    else -> "Antigravity"
}
