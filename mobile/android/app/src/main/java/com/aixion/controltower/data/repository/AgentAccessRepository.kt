package com.aixion.controltower.data.repository

import kotlinx.coroutines.CancellationException
import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.AgentCreateRequestDto
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.core.api.dto.AgentRegistrationResponseDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.LOCAL_APPROVAL_CONNECTOR_PROVIDERS
import com.aixion.controltower.core.api.dto.ProjectDto

data class AgentAccessSummary(
    val agent: ExternalAgentDto,
    val credential: AgentCredentialStatusDto?
) {
    val credentialState: String
        get() = credential?.credential_state ?: "UNKNOWN"
}

class AgentAccessRepository(private val api: ControlTowerApi) {
    suspend fun listApprovalAgents(provider: String): List<AgentAccessSummary> {
        require(provider in LOCAL_APPROVAL_CONNECTOR_PROVIDERS) {
            "Unsupported local approval connector provider"
        }
        return api.listExternalAgents()
            .filter { it.provider == provider }
            .map { agent ->
                val credentials = try {
                    api.getExternalAgentCredentials(agent.id)
                } catch (cancelled: CancellationException) {
                    throw cancelled
                } catch (_: Exception) {
                    null
                }
                AgentAccessSummary(agent, credentials)
            }
    }

    suspend fun listProjects(): List<ProjectDto> = api.listProjects()

    suspend fun createApprovalAgent(
        displayName: String,
        projectId: String,
        provider: String
    ): AgentRegistrationResponseDto {
        require(provider in LOCAL_APPROVAL_CONNECTOR_PROVIDERS) {
            "Unsupported local approval connector provider"
        }
        return api.createExternalAgent(
            AgentCreateRequestDto(
                provider = provider,
                display_name = displayName.trim(),
                allowed_project_ids = listOf(projectId)
            )
        )
    }

    suspend fun rotateToken(agentId: String): AgentRegistrationResponseDto =
        api.rotateExternalAgentToken(agentId)

    suspend fun revokeToken(agentId: String): ExternalAgentDto =
        api.revokeExternalAgentToken(agentId)
}
