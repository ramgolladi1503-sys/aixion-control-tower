package com.aixion.controltower.core.api

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class BackendConnectionConfigTest {
    @Test
    fun normalizeBaseUrlAddsTrailingSlashForLanBackend() {
        assertEquals(
            "http://192.168.1.20:8000/",
            BackendConnectionConfig.normalizeBaseUrl("http://192.168.1.20:8000")
        )
    }

    @Test
    fun normalizeBaseUrlKeepsExistingTrailingSlash() {
        assertEquals(
            "http://10.0.2.2:8000/",
            BackendConnectionConfig.normalizeBaseUrl("http://10.0.2.2:8000/")
        )
    }

    @Test
    fun normalizeBaseUrlTrimsWhitespace() {
        assertEquals(
            "http://192.168.1.20:8000/",
            BackendConnectionConfig.normalizeBaseUrl("  http://192.168.1.20:8000  ")
        )
    }

    @Test
    fun normalizeBaseUrlRejectsBlankUrl() {
        assertThrows(IllegalArgumentException::class.java) {
            BackendConnectionConfig.normalizeBaseUrl("   ")
        }
    }

    @Test
    fun healthUrlShowsPhoneBrowserCheckTarget() {
        assertEquals(
            "http://192.168.1.20:8000/health",
            BackendConnectionConfig.healthUrl("http://192.168.1.20:8000")
        )
    }

    @Test
    fun debugBuildCommandShowsLanOverride() {
        assertEquals(
            "./gradlew assembleDebug -PAIXION_API_BASE_URL=http://192.168.1.20:8000/",
            BackendConnectionConfig.debugBuildCommand("http://192.168.1.20:8000")
        )
    }
}
