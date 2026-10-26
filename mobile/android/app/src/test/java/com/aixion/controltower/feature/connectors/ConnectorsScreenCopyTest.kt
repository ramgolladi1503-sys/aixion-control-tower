package com.aixion.controltower.feature.connectors

import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectorsScreenCopyTest {
    @Test
    fun antigravityGuidanceExplainsSetupAndHostAvailability() {
        val copy = providerSetupDescription("ANTIGRAVITY")

        assertTrue(copy.contains("local pre-tool hook"))
        assertTrue(copy.contains("agent computer"))
        assertTrue(copy.contains("session and API reachable while requests wait"))
        assertFalse(copy.contains("earlier build"))
        assertFalse(copy.contains("phone-tested"))
    }

    @Test
    fun codexGuidanceStatesUnverifiedAvailabilityWithoutClaimingSupport() {
        val copy = providerSetupDescription("CODEX")

        assertTrue(copy.contains("Native Codex phone delivery and same-task return are unverified"))
        assertTrue(copy.contains("does not establish a live approval connection"))
        assertFalse(copy.contains("supported approval path"))
        assertEquals("Unverified · not a live connection", providerSetupStatus("CODEX"))
        assertEquals("View setup only (unverified)", providerSetupButtonLabel("CODEX"))
    }

    @Test
    fun antigravityRetainsProjectHostSetupAffordance() {
        assertTrue(providerSetupDescription("ANTIGRAVITY").contains("agent computer"))
        assertEquals("Supported hook · setup required", providerSetupStatus("ANTIGRAVITY"))
        assertEquals("Open Antigravity setup", providerSetupButtonLabel("ANTIGRAVITY"))
    }
}
