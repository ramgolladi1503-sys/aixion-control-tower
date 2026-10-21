package com.aixion.controltower.data.repository

import kotlinx.coroutines.CancellationException
import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.AgentCreateRequestDto
import com.aixion.controltower.core.api.dto.AgentCredentialStatusDto
import com.aixion.controltower.core.api.dto.AgentRegistrationResponseDto
import com.aixion.controltower.core.api.dto.ExternalAgentDto
import com.aixion.controltower.core.api.dto.ProjectDto

data class AgentAccessSummary(
    val agent: ExternalAgentDto,
    val credential: AgentCredentialStatusDto?
) {
    val credentialState: String
        get() = credential?.credential_state ?: "UNKNOWN"
}

class AgentAccessRepository(private val api: ControlTowerApi) {
    suspend fun listAntigravityAgents(): List<AgentAccessSummary> =
        api.listExternalAgents()
            .filter { it.provider == "ANTIGRAVITY" }
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

    suspend fun listProjects(): List<ProjectDto> = api.listProjects()

    suspend fun createAntigravityAgent(
        displayName: String,
        projectId: String
    ): AgentRegistrationResponseDto = api.createExternalAgent(
        AgentCreateRequestDto(display_name = displayName.trim(), allowed_project_ids = listOf(projectId))
    )

    suspend fun rotateToken(agentId: String): AgentRegistrationResponseDto =
        api.rotateExternalAgentToken(agentId)

    suspend fun revokeToken(agentId: String): ExternalAgentDto =
        api.revokeExternalAgentToken(agentId)
}
