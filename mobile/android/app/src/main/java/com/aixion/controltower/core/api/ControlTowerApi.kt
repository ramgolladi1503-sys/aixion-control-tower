package com.aixion.controltower.core.api

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
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface ControlTowerApi {
    @GET("health")
    suspend fun health(): Map<String, String>

    @POST("auth/register")
    suspend fun register(@Body payload: RegisterRequestDto): AuthResponseDto

    @POST("auth/login")
    suspend fun login(@Body payload: LoginRequestDto): AuthResponseDto

    @GET("auth/me")
    suspend fun me(): AuthUserDto

    @GET("auth/roles")
    suspend fun listRoleChoices(): RoleChoicesDto

    @GET("auth/users")
    suspend fun listUsers(): List<AuthUserDto>

    @PATCH("auth/users/{userId}/role")
    suspend fun updateUserRole(
        @Path("userId") userId: String,
        @Body payload: RoleUpdateRequestDto
    ): AuthUserDto

    @POST("auth/invites")
    suspend fun createInvite(@Body payload: InviteCreateRequestDto): InviteCreateResponseDto

    @GET("auth/invites")
    suspend fun listInvites(): List<InviteDto>

    @POST("auth/invites/{inviteId}/revoke")
    suspend fun revokeInvite(@Path("inviteId") inviteId: String): InviteDto

    @GET("auth/sessions")
    suspend fun listSessions(): List<SessionDto>

    @POST("auth/users/{userId}/sessions/revoke")
    suspend fun revokeUserSessions(@Path("userId") userId: String): SessionRevokeResponseDto

    @GET("projects")
    suspend fun listProjects(): List<ProjectDto>

    @POST("projects")
    suspend fun createProject(@Body payload: ProjectCreateDto): ProjectDto

    @GET("ideas")
    suspend fun listIdeas(): List<IdeaDto>

    @POST("ideas")
    suspend fun createIdea(@Body payload: IdeaCreateDto): IdeaDto

    @GET("work-orders")
    suspend fun listWorkOrders(): List<WorkOrderDto>

    @POST("work-orders")
    suspend fun createWorkOrder(@Body payload: WorkOrderCreateDto): WorkOrderDto

    @GET("approvals")
    suspend fun listApprovals(): List<ApprovalRequestDto>

    @GET("approvals/{approvalId}")
    suspend fun getApproval(@Path("approvalId") approvalId: String): ApprovalRequestDto

    @POST("approvals/{approvalId}/decision")
    suspend fun decideApproval(
        @Path("approvalId") approvalId: String,
        @Body payload: DecisionRequestDto
    ): ApprovalRequestDto

    @GET("test-runs")
    suspend fun listTestRuns(): List<TestRunDto>

    @GET("audit")
    suspend fun listAuditEvents(): List<AuditEventDto>

    @GET("mcp-gateway/pending-requests")
    suspend fun listMCPPendingRequests(
        @Query("project_id") projectId: String? = null,
        @Query("status") status: String? = null,
        @Query("approval_request_id") approvalRequestId: String? = null
    ): List<MCPPendingRequestDto>

    @GET("mcp-gateway/pending-requests/health")
    suspend fun getMCPPendingHealth(
        @Query("project_id") projectId: String? = null
    ): MCPPendingHealthDto

    @GET("mcp-gateway/pending-requests/{pendingRequestId}")
    suspend fun getMCPPendingRequest(
        @Path("pendingRequestId") pendingRequestId: String
    ): MCPPendingRequestDto

    @POST("mcp-gateway/pending-requests/{pendingRequestId}/retry")
    suspend fun retryMCPPendingRequest(
        @Path("pendingRequestId") pendingRequestId: String,
        @Body payload: PendingRetryRequestDto
    ): MCPPendingRequestDto

    @POST("mcp-gateway/approvals/{approvalId}/resolve")
    suspend fun resolveMCPApproval(
        @Path("approvalId") approvalId: String
    ): MCPGatewayDecisionDto
}
