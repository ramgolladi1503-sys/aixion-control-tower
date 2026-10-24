package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.FileChangeSummary
import com.aixion.controltower.core.model.RiskLevel
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ApprovalDecisionPolicyTest {
    private val now = Instant.parse("2026-10-21T12:00:00Z")

    @Test
    fun validManualRequestCanBeReviewedWithoutConnectorProvenance() {
        val request = approval().copy(
            sourceProvider = "MANUAL",
            sourceAgentId = null,
            sourceSessionId = null,
            sourceInvocationId = null,
            verifiedSource = false
        )

        assertNull(ApprovalDecisionPolicy.blockReason(request, now))
    }

    @Test
    fun externalRequestRequiresVerifiedInstallationSessionAndInvocation() {
        assertEquals(
            "The connector installation, session, or invocation identity is missing.",
            ApprovalDecisionPolicy.blockReason(approval().copy(sourceInvocationId = null), now)
        )
        assertEquals(
            "The connector source is not verified. Do not decide on this request.",
            ApprovalDecisionPolicy.blockReason(approval().copy(verifiedSource = false), now)
        )
    }

    @Test
    fun missingMalformedAndElapsedExpiryFailClosed() {
        assertEquals(
            "This request has no valid expiry. Refresh it before deciding.",
            ApprovalDecisionPolicy.blockReason(approval().copy(expiresAt = null), now)
        )
        assertEquals(
            "This request has no valid expiry. Refresh it before deciding.",
            ApprovalDecisionPolicy.blockReason(approval().copy(expiresAt = "not-a-time"), now)
        )
        assertEquals(
            "This request has expired. Refresh the inbox before deciding.",
            ApprovalDecisionPolicy.blockReason(
                approval().copy(expiresAt = "2026-10-21T12:00:00Z"),
                now
            )
        )
    }

    @Test
    fun expiryCountdownUsesReadableUnitsAndExplicitUnavailableStates() {
        assertEquals(
            "Expires in about 2 minutes",
            ApprovalDecisionPolicy.expiryCountdownLabel("2026-10-21T12:01:01Z", now)
        )
        assertEquals(
            "Expires in less than a minute",
            ApprovalDecisionPolicy.expiryCountdownLabel("2026-10-21T12:00:59Z", now)
        )
        assertEquals("Expired", ApprovalDecisionPolicy.expiryCountdownLabel("2026-10-21T12:00:00Z", now))
        assertEquals("Expiry unavailable", ApprovalDecisionPolicy.expiryCountdownLabel(null, now))
        assertEquals("Expiry unavailable", ApprovalDecisionPolicy.expiryCountdownLabel("invalid", now))
    }

    @Test
    fun expiryCountdownUsesHourUnitsAndMarksLastFiveMinutesUrgent() {
        assertEquals(
            "Expires in about 2 hours",
            ApprovalDecisionPolicy.expiryCountdownLabel("2026-10-21T13:01:00Z", now)
        )
        assertEquals(false, ApprovalDecisionPolicy.expiryIsUrgent("2026-10-21T12:06:00Z", now))
        assertEquals(true, ApprovalDecisionPolicy.expiryIsUrgent("2026-10-21T12:05:00Z", now))
        assertEquals(false, ApprovalDecisionPolicy.expiryIsUrgent("invalid", now))
    }

    private fun approval() = ApprovalSummary(
        id = "approval-policy-test",
        projectName = "Test project",
        title = "Permission request",
        summary = "Review this exact request.",
        agentName = "Test agent",
        targetBranch = "main",
        status = ApprovalStatus.REQUESTED,
        risk = RiskLevel.LOW,
        files = listOf(FileChangeSummary("README.md", "tool_call", "{}", RiskLevel.LOW)),
        requiredActions = emptyList(),
        testPlan = emptyList(),
        rollbackPlan = "Deny if uncertain.",
        sourceProvider = "ANTIGRAVITY",
        sourceAgentId = "agent-test",
        sourceSessionId = "session-test",
        sourceInvocationId = "invocation-test",
        expiresAt = "2026-10-21T12:05:00Z",
        verifiedSource = true
    )
}
