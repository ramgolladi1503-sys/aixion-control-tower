package com.aixion.controltower.feature.connectors

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectorsScreenCopyTest {
    @Test
    fun antigravityGuidanceExplainsSetupAndHostAvailability() {
        val copy = providerSetupDescription("ANTIGRAVITY")

        assertTrue(copy.contains("computer running Antigravity"))
        assertTrue(copy.contains("decision can return to the waiting session"))
        assertFalse(copy.contains("earlier build"))
        assertFalse(copy.contains("phone-tested"))
    }

    @Test
    fun codexGuidanceStatesUnverifiedAvailabilityWithoutClaimingSupport() {
        val copy = providerSetupDescription("CODEX")

        assertTrue(copy.contains("not verified yet"))
        assertTrue(copy.contains("Do not rely on this connection for live work"))
    }
}
