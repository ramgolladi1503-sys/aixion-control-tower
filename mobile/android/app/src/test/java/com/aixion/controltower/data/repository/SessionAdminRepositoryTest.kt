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

class SessionAdminRepositoryTest {
    @Test
    fun listSessionsReturnsBackendSessions() = runTest {
        val repository = SessionAdminRepository(SessionAdminApi())

        val sessions = repository.listSessions()

        assertEquals(2, sessions.size)
        assertEquals("owner@example.com", sessions[0].user_email)
        assertEquals(true, sessions[0].active)
    }

    @Test
    fun clearUserSessionsCallsBackendForTargetUser() = runTest {
        val api = SessionAdminApi()
        val repository = SessionAdminRepository(api)

        val response = repository.clearUserSessions("user_reviewer")

        assertEquals("user_reviewer", api.clearedUserId)
        assertEquals("reviewer@example.com", response.target_email)
        assertEquals(2, response.revoked_sessions_count)
    }
}

private class SessionAdminApi : BaseFakeControlTowerApi() {
    var clearedUserId: String? = null

    override suspend fun health(): Map<String, String> = emptyMap()
    override suspend fun register(payload: RegisterRequestDto): AuthResponseDto = throw UnsupportedOperationException("not needed")
    override suspend fun login(payload: LoginRequestDto): AuthResponseDto = throw UnsupportedOperationException("not needed")
    override suspend fun me(): AuthUserDto = throw UnsupportedOperationException("not needed")
    override suspend fun listRoleChoices(): RoleChoicesDto = RoleChoicesDto()
    override suspend fun listUsers(): List<AuthUserDto> = emptyList()
    override suspend fun updateUserRole(userId: String, payload: RoleUpdateRequestDto): AuthUserDto = throw UnsupportedOperationException("not needed")
    override suspend fun createInvite(payload: InviteCreateRequestDto): InviteCreateResponseDto = throw UnsupportedOperationException("not needed")
    override suspend fun listInvites(): List<InviteDto> = emptyList()
    override suspend fun revokeInvite(inviteId: String): InviteDto = throw UnsupportedOperationException("not needed")

    override suspend fun listSessions(): List<SessionDto> {
        return listOf(
            SessionDto(
                id = "session_owner",
                user_id = "user_owner",
                user_email = "owner@example.com",
                user_role = "OWNER",
                active = true,
                revoked = false
            ),
            SessionDto(
                id = "session_reviewer",
                user_id = "user_reviewer",
                user_email = "reviewer@example.com",
                user_role = "REVIEWER",
                active = false,
                revoked = true
            )
        )
    }

    override suspend fun revokeUserSessions(userId: String): SessionRevokeResponseDto {
        clearedUserId = userId
        return SessionRevokeResponseDto(
            target_user_id = userId,
            target_email = "reviewer@example.com",
            revoked_sessions_count = 2
        )
    }

    override suspend fun listProjects(): List<ProjectDto> = emptyList()
    override suspend fun createProject(payload: ProjectCreateDto): ProjectDto = throw UnsupportedOperationException("not needed")
    override suspend fun listIdeas(): List<IdeaDto> = emptyList()
    override suspend fun createIdea(payload: IdeaCreateDto): IdeaDto = throw UnsupportedOperationException("not needed")
    override suspend fun listWorkOrders(): List<WorkOrderDto> = emptyList()
    override suspend fun createWorkOrder(payload: WorkOrderCreateDto): WorkOrderDto = throw UnsupportedOperationException("not needed")
    override suspend fun listApprovals(): List<ApprovalRequestDto> = emptyList()
    override suspend fun getApproval(approvalId: String): ApprovalRequestDto = throw UnsupportedOperationException("not needed")
    override suspend fun decideApproval(approvalId: String, payload: DecisionRequestDto): ApprovalRequestDto = throw UnsupportedOperationException("not needed")
    override suspend fun listTestRuns(): List<TestRunDto> = emptyList()
    override suspend fun listAuditEvents(): List<AuditEventDto> = emptyList()
    override suspend fun listMCPPendingRequests(projectId: String?, status: String?, approvalRequestId: String?): List<MCPPendingRequestDto> = emptyList()
    override suspend fun getMCPPendingHealth(projectId: String?): MCPPendingHealthDto = MCPPendingHealthDto()
    override suspend fun getMCPPendingRequest(pendingRequestId: String): MCPPendingRequestDto = throw UnsupportedOperationException("not needed")
    override suspend fun retryMCPPendingRequest(pendingRequestId: String, payload: PendingRetryRequestDto): MCPPendingRequestDto = throw UnsupportedOperationException("not needed")
    override suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto = throw UnsupportedOperationException("not needed")
}
