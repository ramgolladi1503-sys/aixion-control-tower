package com.aixion.controltower.feature.connectors

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectorsScreenCopyTest {
    @Test
    fun antigravityGuidanceExplainsSetupAndHostAvailability() {
        val copy = providerSetupDescription("ANTIGRAVITY")

        assertTrue(copy.contains("local pre-tool hook is the supported approval path"))
        assertTrue(copy.contains("computer running this project"))
        assertTrue(copy.contains("computer, session, and API reachable"))
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
        assertTrue(providerSetupDescription("ANTIGRAVITY").contains("computer running this project"))
        assertEquals("Supported hook · setup required", providerSetupStatus("ANTIGRAVITY"))
        assertEquals("Open Antigravity setup", providerSetupButtonLabel("ANTIGRAVITY"))
    }
}
