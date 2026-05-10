package com.aixion.controltower.core.model

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ModelsTest {
    @Test
    fun mcpApprovalDetectionUsesMcpFilePath() {
        val approval = approvalWithFiles(
            listOf(
                FileChangeSummary(
                    path = "mcp://child-test-server/update_config",
                    changeType = "update",
                    diff = "MCP tool call requires approval",
                    risk = RiskLevel.HIGH
                )
            )
        )

        assertTrue(approval.isMCPToolApproval)
    }

    @Test
    fun mcpApprovalDetectionUsesMcpToolCallChangeType() {
        val approval = approvalWithFiles(
            listOf(
                FileChangeSummary(
                    path = "virtual/request.json",
                    changeType = "mcp_tool_call",
                    diff = "MCP tool call requires approval",
                    risk = RiskLevel.HIGH
                )
            )
        )

        assertTrue(approval.isMCPToolApproval)
    }

    @Test
    fun ordinaryApprovalIsNotTreatedAsMcpApproval() {
        val approval = approvalWithFiles(
            listOf(
                FileChangeSummary(
                    path = "app/src/main/java/Example.kt",
                    changeType = "update",
                    diff = "ordinary code change",
                    risk = RiskLevel.MEDIUM
                )
            )
        )

        assertFalse(approval.isMCPToolApproval)
    }

    @Test
    fun mcpPendingHealthIsUnhealthyWhenAttentionIsRequired() {
        val health = mcpHealth(attentionRequired = 1)

        assertFalse(health.isHealthy)
    }

    @Test
    fun deadLetterPendingNeedsOperatorAttentionAndIsRetryable() {
        val pending = mcpPending(status = MCPPendingStatus.DEAD_LETTER, lastError = "child server failed")

        assertTrue(pending.needsOperatorAttention)
        assertTrue(pending.isRetryable)
    }

    @Test
    fun forwardedPendingDoesNotNeedOperatorAttentionAndIsNotRetryable() {
        val pending = mcpPending(status = MCPPendingStatus.FORWARDED)

        assertFalse(pending.needsOperatorAttention)
        assertFalse(pending.isRetryable)
    }

    private fun approvalWithFiles(files: List<FileChangeSummary>): ApprovalSummary = ApprovalSummary(
        id = "approval_test",
        projectName = "Demo Project",
        title = "Approve request",
        summary = "Summary",
        agentName = "test-agent",
        targetBranch = "feature/demo",
        status = ApprovalStatus.REQUESTED,
        risk = RiskLevel.HIGH,
        files = files,
        requiredActions = emptyList(),
        testPlan = listOf("Run tests"),
        rollbackPlan = "Revert change"
    )

    private fun mcpPending(
        status: MCPPendingStatus,
        lastError: String? = null
    ): MCPPendingSummary = MCPPendingSummary(
        id = "mcp_pending_test",
        projectId = "project_test",
        approvalRequestId = "approval_test",
        serverName = "child-test-server",
        toolName = "update_config",
        requestedBy = "mcp-client",
        status = status,
        attempts = 0,
        maxAttempts = 3,
        leaseOwner = null,
        leaseExpiresAt = null,
        lastError = lastError,
        createdAt = null,
        updatedAt = null
    )

    private fun mcpHealth(
        attentionRequired: Int = 0,
        expiredLeases: Int = 0,
        deadLetter: Int = 0
    ): MCPPendingHealthSummary = MCPPendingHealthSummary(
        total = 1,
        byStatus = emptyMap(),
        waitingForApproval = 0,
        forwarding = 0,
        activeLeases = 0,
        expiredLeases = expiredLeases,
        retryable = 0,
        deadLetter = deadLetter,
        terminal = 0,
        attentionRequired = attentionRequired,
        oldestWaitingCreatedAt = null,
        oldestDeadLetterCreatedAt = null
    )
}
