package com.aixion.controltower.feature.home

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

class HomeScreenCopyTest {
    @Test
    fun backendBadgeReportsApiStateWithoutClaimingAgentConnection() {
        assertEquals(
            "Syncing",
            homeBackendStatusLabel(
                loading = true,
                refreshing = false,
                hasError = false,
                hasSuccessfulSync = false
            )
        )
        assertEquals(
            "Sync error",
            homeBackendStatusLabel(
                loading = false,
                refreshing = false,
                hasError = true,
                hasSuccessfulSync = false
            )
        )
        assertEquals(
            "Not verified",
            homeBackendStatusLabel(
                loading = false,
                refreshing = false,
                hasError = false,
                hasSuccessfulSync = false
            )
        )
        assertEquals(
            "Syncing",
            homeBackendStatusLabel(
                loading = false,
                refreshing = true,
                hasError = false,
                hasSuccessfulSync = true
            )
        )
        assertEquals(
            "Synced",
            homeBackendStatusLabel(
                loading = false,
                refreshing = false,
                hasError = false,
                hasSuccessfulSync = true
            )
        )
    }

    @Test
    fun allClearRequiresACompletedSuccessfulSync() {
        assertEquals("Status not verified", homeHeroTitle(HomeUiState(loading = false)))
        assertEquals(
            "Refreshing approvals",
            homeHeroTitle(
                HomeUiState(
                    loading = false,
                    refreshing = true,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T00:00:00Z")
                )
            )
        )
        assertEquals(
            "No approvals waiting",
            homeHeroTitle(
                HomeUiState(
                    loading = false,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T00:00:00Z")
                )
            )
        )
        assertEquals(
            "Backend synced",
            homeHeroStatusLabel(
                HomeUiState(
                    loading = false,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T00:00:00Z")
                )
            )
        )
        assertEquals(
            "A fresh backend sync found no requests awaiting your decision. Agent connection status is not available here.",
            homeHeroSubtitle(
                HomeUiState(
                    loading = false,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T00:00:00Z")
                )
            )
        )
    }

    @Test
    fun statusHeroIsHiddenForBackendFailureButRemainsForNormalState() {
        val errorState = HomeUiState(loading = false, errorMessage = "SocketTimeoutException")

        assertFalse(shouldShowHomeStatusHero(errorState))
        assertTrue(shouldShowHomeStatusHero(HomeUiState(loading = false)))
    }
}
