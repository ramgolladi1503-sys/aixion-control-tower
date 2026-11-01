package com.aixion.controltower.feature.home

import com.aixion.controltower.core.model.ApprovalStatus
import com.aixion.controltower.core.model.ApprovalSummary
import com.aixion.controltower.core.model.RiskLevel
import com.aixion.controltower.core.ui.theme.LightTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

class HomeHeroStatusStyleTest {
    private val palette = LightTowerPalette

    @Test
    fun freshEmptyQueueUsesNeutralColor() {
        val state = HomeUiState(
            loading = false,
            lastSuccessfulSyncAt = Instant.parse("2026-10-26T00:00:00Z")
        )

        assertEquals(palette.inkMuted, homeHeroStatusColor(state, palette))
    }

    @Test
    fun unknownOrRefreshingApprovalStateUsesCautionColor() {
        assertEquals(
            palette.caution,
            homeHeroStatusColor(HomeUiState(), palette)
        )
        assertEquals(
            palette.caution,
            homeHeroStatusColor(HomeUiState(loading = false, refreshing = true), palette)
        )
        assertEquals(
            palette.caution,
            homeHeroStatusColor(HomeUiState(loading = false), palette)
        )
    }

    @Test
    fun pendingAndUnsupportedRequestsKeepTheirSemanticColors() {
        val syncedAt = Instant.parse("2026-10-26T00:00:00Z")
        val pending = approval(ApprovalStatus.REQUESTED, RiskLevel.LOW)
        val unsupported = approval(ApprovalStatus.UNKNOWN, RiskLevel.UNKNOWN)

        assertEquals(
            palette.caution,
            homeHeroStatusColor(HomeUiState(loading = false, approvals = listOf(pending), lastSuccessfulSyncAt = syncedAt), palette)
        )
        assertEquals(
            palette.danger,
            homeHeroStatusColor(HomeUiState(loading = false, approvals = listOf(unsupported), lastSuccessfulSyncAt = syncedAt), palette)
        )
    }

    @Test
    fun backendFailureUsesDangerColor() {
        val state = HomeUiState(
            loading = false,
            lastSuccessfulSyncAt = Instant.parse("2026-10-26T00:00:00Z"),
            errorMessage = "Connection unavailable"
        )

        assertEquals(palette.danger, homeHeroStatusColor(state, palette))
    }

    private fun approval(status: ApprovalStatus, risk: RiskLevel) = ApprovalSummary(
        id = "approval-${status.name.lowercase()}",
        projectName = "Example project",
        title = "Review an action",
        summary = "A request needs review.",
        agentName = "Antigravity",
        targetBranch = "main",
        status = status,
        risk = risk,
        files = emptyList(),
        requiredActions = emptyList(),
        testPlan = emptyList(),
        rollbackPlan = ""
    )
}
