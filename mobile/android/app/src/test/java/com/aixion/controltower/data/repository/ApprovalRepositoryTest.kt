package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryEventDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryFileDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryRequestSnapshotDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryResponseDto
import com.aixion.controltower.core.api.dto.ApprovalHistoryRiskDto
import com.aixion.controltower.core.api.dto.AuthResponseDto
import com.aixion.controltower.core.api.dto.AuthUserDto
import com.aixion.controltower.core.api.dto.DecisionRequestDto
import com.aixion.controltower.core.api.dto.LoginRequestDto
import com.aixion.controltower.core.api.dto.MCPGatewayDecisionDto
import com.aixion.controltower.core.api.dto.RegisterRequestDto
import com.aixion.controltower.core.api.dto.RegistrationResponseDto
import com.aixion.controltower.core.model.ApprovalDeliveryStatus
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Test

class ApprovalRepositoryTest {
    @Test
    fun listApprovalHistoryMapsOnlyVerifiedImmutableRequestSnapshot() = runTest {
        val api = HistoryApprovalApi(
            ApprovalHistoryResponseDto(
                events = listOf(
                    ApprovalHistoryEventDto(
                        auditEventId = "audit-1",
                        approvalId = "approval-1",
                        decision = "reject",
                        reason = "The command exceeds scope.",
                        decidedAt = "2026-10-22T10:00:00Z",
                        reviewerLabel = "You",
                        requestSnapshotVerified = true,
                        requestSnapshot = ApprovalHistoryRequestSnapshotDto(
                            projectId = "project-internal-id",
                            title = "Remove production data",
                            summary = "Delete the generated data directory.",
                            agentName = "Antigravity",
                            targetBranch = "main",
                            files = listOf(ApprovalHistoryFileDto("data/output.db", "delete")),
                            testPlan = listOf("Check the database backup."),
                            rollbackPlan = "Restore the backup.",
                            sourceProvider = "ANTIGRAVITY",
                            sourceAgentName = "Antigravity on Mac",
                            sourceSessionId = "private-session-id",
                            sourceInvocationId = "private-invocation-id",
                            sourceTaskUrl = "https://example.invalid/task",
                            risk = ApprovalHistoryRiskDto(level = "HIGH", reasons = listOf("Data loss"))
                        )
                    )
                ),
                limit = 50,
                hasMore = true,
                nextCursor = "opaque-cursor-2"
            )
        )

        val page = ApprovalRepository(api).listApprovalHistory()
        val event = page.events.single()

        assertEquals(50, api.requestedLimit)
        assertEquals(null, api.requestedCursor)
        assertEquals(50, page.limit)
        assertEquals(true, page.hasMore)
        assertEquals("opaque-cursor-2", page.nextCursor)
        assertEquals("reject", event.decision)
        assertEquals("You", event.reviewerLabel)
        assertEquals(true, event.requestSnapshotVerified)
        assertEquals("Remove production data", event.requestSnapshot?.title)
        assertEquals("data/output.db", event.requestSnapshot?.files?.single()?.path)
        assertEquals("HIGH", event.requestSnapshot?.riskLevel?.name)
    }

    @Test
    fun unverifiedHistorySnapshotIsDiscardedAndReviewerEmailIsNeverExposed() = runTest {
        val api = HistoryApprovalApi(
            ApprovalHistoryResponseDto(
                events = listOf(
                    ApprovalHistoryEventDto(
                        auditEventId = "audit-legacy",
                        approvalId = "approval-legacy",
                        decision = "approve",
                        reviewerLabel = "reviewer@example.com",
                        requestSnapshotVerified = false,
                        requestSnapshot = ApprovalHistoryRequestSnapshotDto(title = "Do not display this")
                    )
                )
            )
        )

        val event = ApprovalRepository(api).listApprovalHistory().events.single()

        assertEquals("Workspace reviewer", event.reviewerLabel)
        assertEquals(false, event.requestSnapshotVerified)
        assertEquals(null, event.requestSnapshot)
    }

    @Test
    fun listApprovalHistoryRejectsOutOfRangeLimit() = runTest {
        val repository = ApprovalRepository(HistoryApprovalApi(ApprovalHistoryResponseDto()))

        try {
            repository.listApprovalHistory(limit = 51)
            fail("Expected an invalid history limit to be rejected")
        } catch (_: IllegalArgumentException) {
            // Expected: clients cannot fetch an unbounded history page.
        }
    }

    @Test
    fun listApprovalHistoryForwardsOpaqueCursor() = runTest {
        val api = HistoryApprovalApi(
            ApprovalHistoryResponseDto(events = emptyList(), limit = 50, nextCursor = null)
        )

        val page = ApprovalRepository(api).listApprovalHistory(cursor = "opaque/key+value==")

        assertEquals(50, api.requestedLimit)
        assertEquals("opaque/key+value==", api.requestedCursor)
        assertEquals(null, page.nextCursor)
    }

    @Test
    fun listApprovalHistoryRejectsHasMoreWithoutContinuationCursor() = runTest {
        val repository = ApprovalRepository(
            HistoryApprovalApi(ApprovalHistoryResponseDto(hasMore = true, nextCursor = null))
        )

        try {
            repository.listApprovalHistory()
            fail("Expected a missing continuation cursor to be rejected")
        } catch (_: IllegalStateException) {
            // A has-more response without an opaque cursor cannot be paged safely.
        }
    }

    @Test
    fun deliveryStatusMapsKnownAndUnknownValuesConservatively() = runTest {
        val api = HistoryApprovalApi(
            ApprovalHistoryResponseDto(
                events = listOf(
                    historyStatusDto("not-confirmed", "NOT_CONFIRMED"),
                    historyStatusDto("awaiting", "AWAITING_DELIVERY"),
                    historyStatusDto("expired", "EXPIRED"),
                    historyStatusDto("consumed", "CONSUMED"),
                    historyStatusDto("failure", "DELIVERY_UNCONFIRMED"),
                    historyStatusDto("not-applicable", "NOT_APPLICABLE"),
                    historyStatusDto("unknown", "FUTURE_STATUS"),
                    historyStatusDto("missing", null)
                )
            )
        )

        val statuses = ApprovalRepository(api).listApprovalHistory().events.map { it.deliveryStatus }

        assertEquals(
            listOf(
                ApprovalDeliveryStatus.NOT_CONFIRMED,
                ApprovalDeliveryStatus.AWAITING_DELIVERY,
                ApprovalDeliveryStatus.EXPIRED,
                ApprovalDeliveryStatus.CONSUMED,
                ApprovalDeliveryStatus.DELIVERY_UNCONFIRMED,
                ApprovalDeliveryStatus.NOT_APPLICABLE,
                ApprovalDeliveryStatus.NOT_CONFIRMED,
                ApprovalDeliveryStatus.NOT_CONFIRMED
            ),
            statuses
        )
    }

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

private class HistoryApprovalApi(
    private val response: ApprovalHistoryResponseDto
) : BaseFakeControlTowerApi() {
    var requestedLimit: Int? = null
    var requestedCursor: String? = null

    override suspend fun listApprovalHistory(limit: Int, cursor: String?): ApprovalHistoryResponseDto {
        requestedLimit = limit
        requestedCursor = cursor
        return response
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

private fun historyStatusDto(id: String, deliveryStatus: String?) = ApprovalHistoryEventDto(
    auditEventId = "audit-$id",
    approvalId = "approval-$id",
    decision = "approve",
    deliveryStatus = deliveryStatus
)

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
