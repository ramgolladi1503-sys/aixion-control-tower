package com.aixion.controltower.core.auth

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import retrofit2.HttpException
import retrofit2.Response

class AuthFailureTest {
    @Test
    fun unauthorizedHttpFailureClearsSavedSession() {
        val error = httpError(401)

        assertTrue(AuthFailure.isUnauthorized(error))
        assertTrue(AuthFailure.shouldClearSavedSession(error))
        assertEquals(
            "Session expired or invalid. Open Acct and log in again.",
            AuthFailure.operatorMessage(error)
        )
    }

    @Test
    fun unauthorizedLoginUsesCredentialNeutralGuidance() {
        assertEquals(
            "Email or password was not recognized. Check your details and try again.",
            AuthFailure.loginMessage(httpError(401))
        )
    }

    @Test
    fun nonUnauthorizedLoginUsesTheExistingBackendFailureMessage() {
        val error = IllegalStateException("backend unavailable")

        assertEquals("backend unavailable", AuthFailure.loginMessage(error))
    }

    @Test
    fun nonAuthHttpFailureDoesNotClearSavedSession() {
        val error = httpError(500)

        assertFalse(AuthFailure.isUnauthorized(error))
        assertFalse(AuthFailure.shouldClearSavedSession(error))
    }

    @Test
    fun networkFailureDoesNotClearSavedSession() {
        val error = IllegalStateException("backend unavailable")

        assertFalse(AuthFailure.isUnauthorized(error))
        assertFalse(AuthFailure.shouldClearSavedSession(error))
        assertEquals("backend unavailable", AuthFailure.operatorMessage(error))
    }

    private fun httpError(code: Int): HttpException {
        val body = "{}".toResponseBody("application/json".toMediaType())
        return HttpException(Response.error<Unit>(code, body))
    }
}
