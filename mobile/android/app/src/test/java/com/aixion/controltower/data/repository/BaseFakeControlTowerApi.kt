package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.AuditEventDto
import com.aixion.controltower.core.api.dto.AuthResponseDto
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.ConnectorCreateDto
import com.aixion.controltower.core.api.dto.ConnectorCredentialStatusDto
import com.aixion.controltower.core.api.dto.ConnectorDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperPreviewRequestDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperPreviewResponseDto
import com.aixion.controltower.core.api.dto.ConnectorSchemaMapperStatusDto
import com.aixion.controltower.core.api.dto.ConnectorSecretIssueResponseDto
import com.aixion.controltower.core.api.dto.ConnectorSecretRequestDto
import com.aixion.controltower.core.api.dto.ConnectorSimulationRequestDto
import com.aixion.controltower.core.api.dto.ConnectorSimulationResponseDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateDto
import com.aixion.controltower.core.api.dto.ConnectorTemplateListDto
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.IdeaCreateDto
import com.aixion.controltower.core.api.dto.IdeaDto
import com.aixion.controltower.core.api.dto.InviteCreateRequestDto
import com.aixion.controltower.core.api.dto.InviteCreateResponseDto
import com.aixion.controltower.core.api.dto.InviteDto
import com.aixion.controltower.core.api.dto.LoginRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.api.dto.MCPPendingHealthDto
import com.aixion.controltower.core.api.dto.MCPPendingRequestDto
import com.aixion.controltower.core.api.dto.PendingRetryRequestDto
import com.aixion.controltower.core.api.dto.ProjectCreateDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.api.dto.RegisterRequestDto
import com.aixion.controltower.core.api.dto.RoleChoicesDto
import com.aixion.controltower.core.api.dto.RoleUpdateRequestDto
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.core.api.dto.SessionRevokeResponseDto
import com.aixion.controltower.core.api.dto.TestRunDto
import com.aixion.controltower.core.api.dto.WorkOrderCreateDto
import com.aixion.controltower.core.api.dto.WorkOrderDto

open class BaseFakeControlTowerApi : ControlTowerApi {
    override suspend fun health(): Map<String, String> = emptyMap()
    override suspend fun register(payload: RegisterRequestDto): AuthResponseDto = unsupported()
    override suspend fun login(payload: LoginRequestDto): AuthResponseDto = unsupported()
    override suspend fun me(): AuthUserDto = unsupported()
    override suspend fun listRoleChoices(): RoleChoicesDto = RoleChoicesDto()
    override suspend fun listUsers(): List<AuthUserDto> = emptyList()
    override suspend fun updateUserRole(userId: String, payload: RoleUpdateRequestDto): AuthUserDto = unsupported()
    override suspend fun createInvite(payload: InviteCreateRequestDto): InviteCreateResponseDto = unsupported()
    override suspend fun listInvites(): List<InviteDto> = emptyList()
    override suspend fun revokeInvite(inviteId: String): InviteDto = unsupported()
    override suspend fun listSessions(): List<SessionDto> = emptyList()
    override suspend fun revokeUserSessions(userId: String): SessionRevokeResponseDto = unsupported()
    override suspend fun listProjects(): List<ProjectDto> = emptyList()
    override suspend fun createProject(payload: ProjectCreateDto): ProjectDto = unsupported()
    override suspend fun listIdeas(): List<IdeaDto> = emptyList()
    override suspend fun createIdea(payload: IdeaCreateDto): IdeaDto = unsupported()
    override suspend fun listWorkOrders(): List<WorkOrderDto> = emptyList()
    override suspend fun createWorkOrder(payload: WorkOrderCreateDto): WorkOrderDto = unsupported()
    override suspend fun listApprovals(): List<ApprovalRequestDto> = emptyList()
    override suspend fun getApproval(approvalId: String): ApprovalRequestDto = unsupported()
    override suspend fun decideApproval(approvalId: String, payload: DecisionRequestDto): ApprovalRequestDto = unsupported()
    override suspend fun listTestRuns(): List<TestRunDto> = emptyList()
    override suspend fun listAuditEvents(): List<AuditEventDto> = emptyList()

    override suspend fun listConnectors(): List<ConnectorDto> = emptyList()
    override suspend fun createConnector(payload: ConnectorCreateDto): ConnectorDto = unsupported()
    override suspend fun listConnectorTemplates(): ConnectorTemplateListDto = ConnectorTemplateListDto()
    override suspend fun getConnectorTemplate(templateId: String): ConnectorTemplateDto = unsupported()
    override suspend fun enableConnector(connectorId: String): ConnectorDto = unsupported()
    override suspend fun disableConnector(connectorId: String): ConnectorDto = unsupported()
    override suspend fun getConnectorCredentials(connectorId: String): ConnectorCredentialStatusDto = unsupported()
    override suspend fun issueConnectorSecret(connectorId: String, payload: ConnectorSecretRequestDto): ConnectorSecretIssueResponseDto = unsupported()
    override suspend fun rotateConnectorSecret(connectorId: String, payload: ConnectorSecretRequestDto): ConnectorSecretIssueResponseDto = unsupported()
    override suspend fun revokeConnectorSecret(connectorId: String): ConnectorDto = unsupported()
    override suspend fun getConnectorSchemaMapper(connectorId: String): ConnectorSchemaMapperStatusDto = unsupported()
    override suspend fun updateConnectorSchemaMapper(connectorId: String, payload: ConnectorSchemaMapperDto): ConnectorSchemaMapperStatusDto = unsupported()
    override suspend fun previewConnectorSchemaMapper(
        connectorId: String,
        payload: ConnectorSchemaMapperPreviewRequestDto
    ): ConnectorSchemaMapperPreviewResponseDto = unsupported()

    override suspend fun simulateConnectorWebhook(
        connectorId: String,
        payload: ConnectorSimulationRequestDto
    ): ConnectorSimulationResponseDto = unsupported()

    override suspend fun listMCPPendingRequests(
        projectId: String?,
        status: String?,
        approvalRequestId: String?
    ): List<MCPPendingRequestDto> = emptyList()

    override suspend fun getMCPPendingHealth(projectId: String?): MCPPendingHealthDto = MCPPendingHealthDto()
    override suspend fun getMCPPendingRequest(pendingRequestId: String): MCPPendingRequestDto = unsupported()
    override suspend fun retryMCPPendingRequest(pendingRequestId: String, payload: PendingRetryRequestDto): MCPPendingRequestDto = unsupported()
    override suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto = unsupported()

    private fun <T> unsupported(): T = throw UnsupportedOperationException("not needed")
}
