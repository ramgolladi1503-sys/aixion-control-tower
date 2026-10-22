package com.aixion.controltower.data.repository

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthRepositoryLogoutTest {
    @Test
    fun successfulServerRevocationAndPushCleanupClearLocalSession() = runTest {
        var cleared = false

        val result = logoutWithCleanup(
            unregisterPushDevice = { true },
            revokeServerSession = {},
            clearLocalSession = { cleared = true },
            onFailure = { _, _ -> error("unexpected failure") }
        )

        assertTrue(result)
        assertTrue(cleared)
    }

    @Test
    fun serverFailureStillClearsLocalSessionAndReportsIncompleteCleanup() = runTest {
        var cleared = false
        var revokedAttempted = false
        val failures = mutableListOf<String>()

        val result = logoutWithCleanup(
            unregisterPushDevice = { true },
            revokeServerSession = {
                revokedAttempted = true
                throw IllegalStateException("network unavailable")
            },
            clearLocalSession = { cleared = true },
            onFailure = { stage, _ -> failures += stage }
        )

        assertFalse(result)
        assertTrue(revokedAttempted)
        assertTrue(cleared)
        assertEquals(listOf("server-session-revocation"), failures)
    }

    @Test
    fun pushCleanupFailureDoesNotSkipServerRevocationOrLocalClear() = runTest {
        var revoked = false
        var cleared = false

        val result = logoutWithCleanup(
            unregisterPushDevice = { false },
            revokeServerSession = { revoked = true },
            clearLocalSession = { cleared = true },
            onFailure = { _, _ -> error("a false result is reported by the caller") }
        )

        assertFalse(result)
        assertTrue(revoked)
        assertTrue(cleared)
    }

    @Test
    fun cancellationStillClearsLocalSession() = runTest {
        var cleared = false

        val failure = runCatching {
            logoutWithCleanup(
                unregisterPushDevice = { throw CancellationException("cancelled") },
                revokeServerSession = {},
                clearLocalSession = { cleared = true },
                onFailure = { _, _ -> error("unexpected failure") }
            )
        }.exceptionOrNull()

        assertTrue(failure is CancellationException)
        assertTrue(cleared)
    }
}
