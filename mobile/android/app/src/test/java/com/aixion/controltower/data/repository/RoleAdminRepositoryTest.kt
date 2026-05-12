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

class RoleAdminRepositoryTest {
    @Test
    fun listRolesReturnsBackendRoleChoices() = runTest {
        val repository = RoleAdminRepository(RoleAdminApi())

        assertEquals(listOf("OWNER", "MAINTAINER", "REVIEWER"), repository.listRoles())
    }

    @Test
    fun listUsersReturnsBackendUsers() = runTest {
        val repository = RoleAdminRepository(RoleAdminApi())

        val users = repository.listUsers()

        assertEquals(2, users.size)
        assertEquals("owner@example.com", users[0].email)
        assertEquals("REVIEWER", users[1].role)
    }

    @Test
    fun updateRoleSendsRequestedRole() = runTest {
        val api = RoleAdminApi()
        val repository = RoleAdminRepository(api)

        val updated = repository.updateRole("user_reviewer", "MAINTAINER")

        assertEquals("user_reviewer", api.updatedUserId)
        assertEquals("MAINTAINER", api.updatedRole)
        assertEquals("MAINTAINER", updated.role)
    }
}

private class RoleAdminApi : ControlTowerApi {
    var updatedUserId: String? = null
    var updatedRole: String? = null

    override suspend fun health(): Map<String, String> = emptyMap()

    override suspend fun register(payload: RegisterRequestDto): AuthResponseDto {
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

    override suspend fun listUsers(): List<AuthUserDto> {
        return listOf(
            AuthUserDto(
                id = "user_owner",
                email = "owner@example.com",
                display_name = "Owner",
                role = "OWNER"
            ),
            AuthUserDto(
                id = "user_reviewer",
                email = "reviewer@example.com",
                display_name = "Reviewer",
                role = "REVIEWER"
            )
        )
    }

    override suspend fun updateUserRole(userId: String, payload: RoleUpdateRequestDto): AuthUserDto {
        updatedUserId = userId
        updatedRole = payload.role
        return AuthUserDto(
            id = userId,
            email = "reviewer@example.com",
            display_name = "Reviewer",
            role = payload.role
        )
    }

    override suspend fun createInvite(payload: InviteCreateRequestDto): InviteCreateResponseDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun listInvites(): List<InviteDto> = emptyList()

    override suspend fun revokeInvite(inviteId: String): InviteDto {
        throw UnsupportedOperationException("not needed")
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
