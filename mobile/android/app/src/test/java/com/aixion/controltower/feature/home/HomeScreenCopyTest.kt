package com.aixion.controltower.feature.home

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

class HomeScreenCopyTest {
    @Test
    fun backendBadgeReportsApiStateWithoutClaimingAgentConnection() {
        assertEquals(
            "SYNCING",
            homeBackendStatusLabel(
                loading = true,
                refreshing = false,
                hasError = false,
                hasSuccessfulSync = false
            )
        )
        assertEquals(
            "SYNC ERROR",
            homeBackendStatusLabel(
                loading = false,
                refreshing = false,
                hasError = true,
                hasSuccessfulSync = false
            )
        )
        assertEquals(
            "NOT VERIFIED",
            homeBackendStatusLabel(
                loading = false,
                refreshing = false,
                hasError = false,
                hasSuccessfulSync = false
            )
        )
        assertEquals(
            "SYNCING",
            homeBackendStatusLabel(
                loading = false,
                refreshing = true,
                hasError = false,
                hasSuccessfulSync = true
            )
        )
        assertEquals(
            "SYNCED",
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
            "Refreshing command state",
            homeHeroTitle(
                HomeUiState(
                    loading = false,
                    refreshing = true,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T00:00:00Z")
                )
            )
        )
        assertEquals(
            "All clear",
            homeHeroTitle(
                HomeUiState(
                    loading = false,
                    lastSuccessfulSyncAt = Instant.parse("2026-10-22T00:00:00Z")
                )
            )
        )
    }
}
