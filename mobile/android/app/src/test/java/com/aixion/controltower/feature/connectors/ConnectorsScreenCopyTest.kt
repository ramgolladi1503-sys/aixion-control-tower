package com.aixion.controltower.feature.connectors

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
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

        assertTrue(copy.contains("phone approval delivery and returning a decision to the same task are not verified"))
        assertTrue(copy.contains("does not establish a live approval connection"))
        assertTrue(copy.contains("Do not rely on it for live work"))
        assertEquals("Unverified · not a live connection", providerSetupStatus("CODEX"))
        assertEquals("View setup only (unverified)", providerSetupButtonLabel("CODEX"))
    }

    @Test
    fun antigravityRetainsProjectHostSetupAffordance() {
        assertTrue(providerSetupDescription("ANTIGRAVITY").contains("computer running Antigravity"))
        assertEquals("Not verified", providerSetupStatus("ANTIGRAVITY"))
        assertEquals("Open Antigravity setup", providerSetupButtonLabel("ANTIGRAVITY"))
    }
}
