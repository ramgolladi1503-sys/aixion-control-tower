package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.AuditEventDto
import com.aixion.controltower.core.api.dto.AuthResponseDto
import com.aixion.controltower.core.api.dto.AuthUserDto
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
import com.aixion.controltower.core.api.dto.RegistrationResponseDto
import com.aixion.controltower.core.api.dto.RoleChoicesDto
import com.aixion.controltower.core.api.dto.RoleUpdateRequestDto
import com.aixion.controltower.core.api.dto.SessionDto
import com.aixion.controltower.core.api.dto.SessionRevokeResponseDto
import com.aixion.controltower.core.api.dto.TestRunDto
import com.aixion.controltower.core.api.dto.WorkOrderCreateDto
import com.aixion.controltower.core.api.dto.WorkOrderDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

class InviteAdminRepositoryTest {
    @Test
    fun createInviteSendsEmailRoleAndExpiry() = runTest {
        val api = InviteAdminApi()
        val repository = InviteAdminRepository(api)

        val created = repository.createInvite(" new@example.com ", "MAINTAINER", expiresInDays = 14)

        assertEquals("new@example.com", api.createdPayload?.email)
        assertEquals("MAINTAINER", api.createdPayload?.role)
        assertEquals(14, api.createdPayload?.expires_in_days)
        assertEquals("token_once", created.token)
    }

    @Test
    fun listInvitesReturnsBackendInvites() = runTest {
        val repository = InviteAdminRepository(InviteAdminApi())

        val invites = repository.listInvites()

        assertEquals(2, invites.size)
        assertEquals("new@example.com", invites[0].email)
        assertEquals("PENDING", invites[0].status)
    }

    @Test
    fun revokeInviteCallsBackendAndReturnsUpdatedInvite() = runTest {
        val api = InviteAdminApi()
        val repository = InviteAdminRepository(api)

        val revoked = repository.revokeInvite("invite_pending")

        assertEquals("invite_pending", api.revokedInviteId)
        assertEquals("REVOKED", revoked.status)
    }
}

private class InviteAdminApi : BaseFakeControlTowerApi() {
    var createdPayload: InviteCreateRequestDto? = null
    var revokedInviteId: String? = null

    override suspend fun health(): Map<String, String> = emptyMap()

    override suspend fun register(payload: RegisterRequestDto): RegistrationResponseDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun login(payload: LoginRequestDto): AuthResponseDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun me(): AuthUserDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listRoleChoices(): RoleChoicesDto {
        return RoleChoicesDto(roles = listOf("OWNER", "MAINTAINER", "REVIEWER"))
    }

    override suspend fun listUsers(): List<AuthUserDto> = emptyList()

    override suspend fun updateUserRole(userId: String, payload: RoleUpdateRequestDto): AuthUserDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun createInvite(payload: InviteCreateRequestDto): InviteCreateResponseDto {
        createdPayload = payload
        return InviteCreateResponseDto(
            id = "invite_new",
            email = payload.email,
            role = payload.role,
            status = "PENDING",
            created_by_user_id = "user_owner",
            token = "token_once"
        )
    }

    override suspend fun listInvites(): List<InviteDto> {
        return listOf(
            InviteDto(
                id = "invite_pending",
                email = "new@example.com",
                role = "REVIEWER",
                status = "PENDING",
                created_by_user_id = "user_owner"
            ),
            InviteDto(
                id = "invite_accepted",
                email = "accepted@example.com",
                role = "MAINTAINER",
                status = "ACCEPTED",
                created_by_user_id = "user_owner",
                accepted_by_user_id = "user_accepted"
            )
        )
    }

    override suspend fun revokeInvite(inviteId: String): InviteDto {
        revokedInviteId = inviteId
        return InviteDto(
            id = inviteId,
            email = "new@example.com",
            role = "REVIEWER",
            status = "REVOKED",
            created_by_user_id = "user_owner"
        )
    }

    override suspend fun listSessions(): List<SessionDto> = emptyList()

    override suspend fun revokeUserSessions(userId: String): SessionRevokeResponseDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listProjects(): List<ProjectDto> = emptyList()

    override suspend fun createProject(payload: ProjectCreateDto): ProjectDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listIdeas(): List<IdeaDto> = emptyList()

    override suspend fun createIdea(payload: IdeaCreateDto): IdeaDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listWorkOrders(): List<WorkOrderDto> = emptyList()

    override suspend fun createWorkOrder(payload: WorkOrderCreateDto): WorkOrderDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listApprovals(): List<ApprovalRequestDto> = emptyList()

    override suspend fun getApproval(approvalId: String): ApprovalRequestDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun decideApproval(
        approvalId: String,
        payload: DecisionRequestDto
    ): ApprovalRequestDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listTestRuns(): List<TestRunDto> = emptyList()

    override suspend fun listAuditEvents(): List<AuditEventDto> = emptyList()

    override suspend fun listMCPPendingRequests(
        projectId: String?,
        status: String?,
        approvalRequestId: String?
    ): List<MCPPendingRequestDto> = emptyList()

    override suspend fun getMCPPendingHealth(projectId: String?): MCPPendingHealthDto = MCPPendingHealthDto()

    override suspend fun getMCPPendingRequest(pendingRequestId: String): MCPPendingRequestDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun retryMCPPendingRequest(
        pendingRequestId: String,
        payload: PendingRetryRequestDto
    ): MCPPendingRequestDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto {
        throw UnsupportedOperationException("not needed")
    }
}
