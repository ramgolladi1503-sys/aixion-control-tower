package com.aixion.controltower.data.repository

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthSessionEstablishmentTest {
    @Test
    fun failedServerValidationClearsNewTokenAndPreservesOriginalFailure() = runTest {
        var savedToken: String? = null
        var cleared = false
        val validationFailure = IllegalStateException("session rejected")

        val actualFailure = runCatching {
            establishAuthenticatedSession(
                token = "new-session-token",
                saveToken = { savedToken = it },
                validateSession = { throw validationFailure },
                clearToken = { cleared = true }
            )
        }.exceptionOrNull()

        assertSame(validationFailure, actualFailure)
        assertEquals("new-session-token", savedToken)
        assertTrue(cleared)
    }

    @Test
    fun validServerSessionKeepsNewToken() = runTest {
        var savedToken: String? = null
        var cleared = false

        val label = establishAuthenticatedSession(
            token = "new-session-token",
            saveToken = { savedToken = it },
            validateSession = { "Operator • operator@example.test • OWNER" },
            clearToken = { cleared = true }
        )

        assertEquals("Operator • operator@example.test • OWNER", label)
        assertEquals("new-session-token", savedToken)
        assertFalse(cleared)
    }

    @Test
    fun storageFailureAlsoAttemptsToClearPossiblyPartialToken() = runTest {
        var cleared = false
        val storageFailure = IllegalStateException("preference commit failed")

        val actualFailure = runCatching {
            establishAuthenticatedSession(
                token = "new-session-token",
                saveToken = { throw storageFailure },
                validateSession = { "unreachable" },
                clearToken = { cleared = true }
            )
        }.exceptionOrNull()

        assertSame(storageFailure, actualFailure)
        assertTrue(cleared)
    }
}
