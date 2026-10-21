package com.aixion.controltower.feature.home

import org.junit.Assert.assertEquals
import org.junit.Test

class HomeScreenCopyTest {
    @Test
    fun backendBadgeReportsApiStateWithoutClaimingAgentConnection() {
        assertEquals("SYNCING", homeBackendStatusLabel(loading = true, hasError = false))
        assertEquals("SYNC ERROR", homeBackendStatusLabel(loading = false, hasError = true))
        assertEquals("SYNCED", homeBackendStatusLabel(loading = false, hasError = false))
    }
}
