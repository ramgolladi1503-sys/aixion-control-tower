package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.AuthResponseDto
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.LoginRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.api.dto.RegisterRequestDto
import com.aixion.controltower.core.api.dto.RegistrationResponseDto
import kotlinx.coroutines.test.runTest
import org.junit.Assert.fail
import org.junit.Test

class ApprovalRepositoryTest {
    @Test
    fun listApprovalsThrowsWhenBackendListFails() = runTest {
        val repository = ApprovalRepository(FailingApprovalApi())

        assertIllegalStateFailure {
            repository.listApprovals()
        }
    }

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
}

private suspend fun assertIllegalStateFailure(block: suspend () -> Unit) {
    try {
        block()
    } catch (error: IllegalStateException) {
        return
    }

    fail("Expected IllegalStateException to be thrown")
}

private class FailingApprovalApi : BaseFakeControlTowerApi() {
    override suspend fun register(payload: RegisterRequestDto): RegistrationResponseDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun login(payload: LoginRequestDto): AuthResponseDto {
        throw UnsupportedOperationException("not needed")
    }

    override suspend fun me(): AuthUserDto {
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

    override suspend fun resolveMCPApproval(approvalId: String): MCPGatewayDecisionDto {
        throw IllegalStateException("backend resolve failed")
    }
}
