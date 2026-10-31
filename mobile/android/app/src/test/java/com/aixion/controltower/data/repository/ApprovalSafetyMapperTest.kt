package com.aixion.controltower.data.repository

import com.aixion.controltower.core.api.dto.ApprovalRequestDto
import com.aixion.controltower.core.api.dto.FileChangeDto
import com.aixion.controltower.core.api.dto.RiskAssessmentDto
import com.aixion.controltower.core.model.ApprovalDashboardBucket
import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.model.dashboardBucket
import com.aixion.controltower.core.model.requiresHumanAction
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ApprovalSafetyMapperTest {
    @Test
    fun unknownBackendStatusDoesNotBecomeAnActionableRequest() {
        val approval = approval(status = "WAITING_FOR_NEW_REVIEW_STATE", risk = "LOW")
            .toUiSummary()

        assertEquals(ApprovalStatus.UNKNOWN, approval.status)
        assertEquals(ApprovalDashboardBucket.HISTORY, approval.dashboardBucket)
        assertFalse(approval.requiresHumanAction)
    }

    @Test
    fun unknownBackendRiskDoesNotBecomeMediumRiskOrActionable() {
        val approval = approval(status = "REQUESTED", risk = "EMERGENCY_UNKNOWN")
            .toUiSummary()

        assertEquals(ApprovalStatus.REQUESTED, approval.status)
        assertEquals(RiskLevel.UNKNOWN, approval.risk)
        assertFalse(approval.requiresHumanAction)
    }

    @Test
    fun approvalMappingPreservesProjectIdentityAndRiskRationale() {
        val approval = approval(
            status = "PENDING_REVIEW",
            risk = "CRITICAL",
            reasons = listOf("Antigravity command can cause broad side effects.")
        ).toUiSummary()

        assertEquals("project_mapper_test", approval.projectId)
        assertEquals(
            listOf("Antigravity command can cause broad side effects."),
            approval.riskReasons
        )
    }

    @Test
    fun approvalMappingPreservesInvocationIdentityAndExpiryVerbatim() {
        val approval = approval(
            status = "PENDING_REVIEW",
            risk = "HIGH"
        ).copy(
            source_invocation_id = "antigravity:session-7:tool-19",
            expires_at = "2026-10-21T12:30:00Z"
        ).toUiSummary()

        assertEquals("antigravity:session-7:tool-19", approval.sourceInvocationId)
        assertEquals("2026-10-21T12:30:00Z", approval.expiresAt)
    }

    @Test
    fun approvalMappingPreservesExecutableFileContentSeparatelyFromDiff() {
        val approval = approval(status = "PENDING_REVIEW", risk = "HIGH").copy(
            files = listOf(
                FileChangeDto(
                    path = "antigravity://tool/write_to_file",
                    change_type = "tool_call",
                    diff = "harmless displayed diff",
                    new_content = "actual executable content"
                )
            )
        ).toUiSummary()

        assertEquals("harmless displayed diff", approval.files.single().diff)
        assertEquals("actual executable content", approval.files.single().newContent)
    }
}

private fun approval(
    status: String,
    risk: String,
    reasons: List<String> = emptyList()
) = ApprovalRequestDto(
    id = "approval_mapper_test",
    project_id = "project_mapper_test",
    title = "Mapper safety test",
    summary = "Unknown API values must not enable decisions.",
    target_branch = "main",
    status = status,
    risk = RiskAssessmentDto(level = risk, reasons = reasons)
)
