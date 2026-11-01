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
    fun codexGuidanceOffersTheTestedCliHookAndKeepsDesktopUnverified() {
        val copy = providerSetupDescription("CODEX")

        assertTrue(copy.contains("Codex CLI PermissionRequest hook"))
        assertTrue(copy.contains("Setup is required for each project"))
        assertTrue(copy.contains("Codex Desktop is not verified"))
        assertTrue(copy.contains("cannot confirm that your hook is installed"))
        assertEquals("Codex CLI hook · setup required", providerSetupStatus("CODEX"))
        assertEquals("Open Codex CLI setup", providerSetupButtonLabel("CODEX"))
    }

    @Test
    fun antigravityRetainsProjectHostSetupAffordance() {
        assertTrue(providerSetupDescription("ANTIGRAVITY").contains("agent computer"))
        assertEquals("Supported hook · setup required", providerSetupStatus("ANTIGRAVITY"))
        assertEquals("Open Antigravity setup", providerSetupButtonLabel("ANTIGRAVITY"))
    }
}
