package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.ControlTowerApi
import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.AuditEventDto
import com.aixion.controltower.core.api.dto.AuthResponseDto
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.IdeaCreateDto
import com.aixion.controltower.core.api.dto.IdeaDto
import com.aixion.controltower.core.api.dto.LoginRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.api.dto.MCPPendingHealthDto
import com.aixion.controltower.core.api.dto.MCPPendingRequestDto
import com.aixion.controltower.core.api.dto.PendingRetryRequestDto
import com.aixion.controltower.core.api.dto.ProjectCreateDto
import com.aixion.controltower.core.api.dto.ProjectDto
import com.aixion.controltower.core.api.dto.RegisterRequestDto
import com.aixion.controltower.core.api.dto.TestRunDto
import com.aixion.controltower.core.api.dto.WorkOrderCreateDto
import com.aixion.controltower.core.api.dto.WorkOrderDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

class ApprovalRepositoryTest {
    @Test
    fun getApprovalThrowsWhenBackendFetchFails() = runTest {
        val repository = ApprovalRepository(FailingApprovalApi())

        assertIllegalStateFailure {
            repository.getApproval("approval_missing")
        }
    }

    @Test
    fun decideThrowsWhenBackendDecisionFails() = runTest {
        val repository = ApprovalRepository(FailingApprovalApi())

        assertIllegalStateFailure {
            repository.decide("approval_missing", "approve", "test")
        }
    }

    @Test
    fun resolveMCPApprovalThrowsWhenBackendResolveFails() = runTest {
        val repository = ApprovalRepository(FailingApprovalApi())

        assertIllegalStateFailure {
            repository.resolveMCPApproval("approval_missing")
        }
    }

    @Test
    fun listApprovalsStillUsesMockFallbackForReadOnlyPreview() = runTest {
        val repository = ApprovalRepository(FailingApprovalApi())

        val approvals = repository.listApprovals()

        assertEquals("approval_critical", approvals.first().id)
    }
}

private suspend fun assertIllegalStateFailure(block: suspend () -> Unit) {
    try {
        block()
    } catch (error: IllegalStateException) {
        return
    }

    fail("Expected IllegalStateException to be thrown")
}

private class FailingApprovalApi : ControlTowerApi {
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

    override suspend fun listApprovals(): List<ApprovalRequestDto> {
        throw IllegalStateException("backend list failed")
    }

    override suspend fun getApproval(approvalId: String): ApprovalRequestDto {
        throw IllegalStateException("backend approval fetch failed")
    }

    override suspend fun decideApproval(
        approvalId: String,
        payload: DecisionRequestDto
    ): ApprovalRequestDto {
        throw IllegalStateException("backend decision failed")
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
        throw IllegalStateException("backend resolve failed")
    }
}
