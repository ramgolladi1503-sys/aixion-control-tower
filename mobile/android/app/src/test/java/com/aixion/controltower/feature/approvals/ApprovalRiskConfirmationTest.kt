package com.aixion.controltower.feature.approvals

import com.aixion.controltower.core.model.RiskLevel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ApprovalRiskConfirmationTest {
    @Test
    fun highAndCriticalApprovalsRequireExplicitConfirmation() {
        assertTrue(requiresExplicitRiskConfirmation(RiskLevel.HIGH))
        assertTrue(requiresExplicitRiskConfirmation(RiskLevel.CRITICAL))
    }

    @Test
    fun otherRiskLevelsDoNotUseTheHighRiskConfirmationPath() {
        assertFalse(requiresExplicitRiskConfirmation(RiskLevel.LOW))
        assertFalse(requiresExplicitRiskConfirmation(RiskLevel.MEDIUM))
        assertFalse(requiresExplicitRiskConfirmation(RiskLevel.BLOCKED))
        assertFalse(requiresExplicitRiskConfirmation(RiskLevel.UNKNOWN))
    }

    @Test
    fun confirmationIdentifierIsShortAndFiltersUnsafeCharacters() {
        assertEquals("tool-991", shortSafeApprovalId("connector:session:tool-991"))
        assertEquals("unavailable", shortSafeApprovalId("••••"))
    }
}
