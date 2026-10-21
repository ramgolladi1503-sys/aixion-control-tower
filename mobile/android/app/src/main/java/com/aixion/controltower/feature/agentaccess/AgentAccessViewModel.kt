package com.aixion.controltower.feature.agentaccess

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aixion.controltower.core.api.ApiClient
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.core.api.dto.AgentRegistrationResponseDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.isExpectedAntigravityApprovalAgent
import com.aixion.controltower.data.repository.AgentAccessRepository
import com.aixion.controltower.data.repository.AgentAccessSummary
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

data class AgentAccessUiState(
    val loading: Boolean = true,
    val working: Boolean = false,
    val agents: List<AgentAccessSummary> = emptyList(),
    val projects: List<ProjectDto> = emptyList(),
    val selectedProjectId: String? = null,
    val displayName: String = "Antigravity on this Mac",
    val oneTimeToken: String? = null,
    val oneTimeAgentName: String? = null,
    val oneTimeAgentId: String? = null,
    val oneTimeProjectId: String? = null,
    val notice: String = "",
    val error: String = ""
)

class AgentAccessViewModel(application: Application) : AndroidViewModel(application) {
    private val repository = AgentAccessRepository(ApiClient.create(application.applicationContext))
    private val _state = MutableStateFlow(AgentAccessUiState())
    val state: StateFlow<AgentAccessUiState> = _state.asStateFlow()

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.value = _state.value.copy(loading = true, error = "")
            try {
                val projects = repository.listProjects()
                val agents = repository.listAntigravityAgents()
                val current = _state.value
                _state.value = current.copy(
                    loading = false,
                    projects = projects,
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
                    error = "Could not load projects or Antigravity agents. Check owner access and connection, then retry."
                )
            }
        }
    }

    fun setDisplayName(value: String) {
        _state.value = _state.value.copy(displayName = value.take(100), error = "")
    }

    fun selectProject(projectId: String) {
        if (_state.value.projects.any { it.id == projectId }) {
            _state.value = _state.value.copy(selectedProjectId = projectId, error = "")
        }
    }

    fun createAgent() {
        val current = _state.value
        val name = current.displayName.trim()
        val projectId = current.selectedProjectId
        if (name.isBlank() || projectId == null || current.working) {
            _state.value = current.copy(error = "Enter an agent name and select one project.")
            return
        }
        runCredentialAction("Antigravity agent created", expectedProjectId = projectId) {
            repository.createAntigravityAgent(name, projectId)
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
        runCredentialAction("Antigravity token rotated", expectedProjectId = projectId, expectedAgentId = agentId) {
            repository.rotateToken(agentId)
        }
    }

    fun revokeToken(agentId: String) {
        if (_state.value.working) return
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
                    oneTimeAgentName = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeAgentName,
                    oneTimeAgentId = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeAgentId,
                    oneTimeProjectId = if (_state.value.oneTimeAgentId == agentId) null else _state.value.oneTimeProjectId,
                    notice = "Agent token revoked. The Mac hook will now fail closed."
                )
                try {
                    _state.value = _state.value.copy(agents = repository.listAntigravityAgents())
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
            oneTimeAgentName = null,
            oneTimeAgentId = null,
            oneTimeProjectId = null
        )
    }

    private fun runCredentialAction(
        successMessage: String,
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
                    !isExpectedAntigravityApprovalAgent(agent, expectedProjectId) ||
                    (expectedAgentId != null && agent.id != expectedAgentId)
                ) {
                    _state.value = _state.value.copy(
                        working = false,
                        error = "Server response did not confirm the exact Antigravity project, actions, and credential. Do not install this token; refresh and reconcile agent state."
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
                    oneTimeAgentName = agent.display_name,
                    oneTimeAgentId = agent.id,
                    oneTimeProjectId = expectedProjectId,
                    notice = "$successMessage. Copy the token into macOS Keychain now; it will not be shown again.",
                    error = ""
                )
                try {
                    val agents = repository.listAntigravityAgents()
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
