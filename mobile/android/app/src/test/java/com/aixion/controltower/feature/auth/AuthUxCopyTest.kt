package com.aixion.controltower.feature.auth

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthUxCopyTest {
    @Test
    fun unauthenticatedStateExplainsSignInFirst() {
        val state = AuthUiState(email = "owner@example.com", password = "StrongPass123!", sessionChecked = true)

        assertEquals("Sign in to continue", AuthUxCopy.heroTitle(state))
        assertEquals("No active session", AuthUxCopy.sessionLabel(state))
        assertTrue(AuthUxCopy.canAttemptLogin(state))
        assertTrue(AuthUxCopy.canAttemptRegister(state))
    }

    @Test
    fun verificationRequiredBlocksLoginAndGuidesUserToEmailCode() {
        val state = AuthUiState(
            email = "owner@example.com",
            password = "StrongPass123!",
            verificationRequired = true,
            devVerificationCode = "123456"
        )

        assertEquals("Check your email", AuthUxCopy.heroTitle(state))
        assertEquals("Verification required", AuthUxCopy.sessionLabel(state))
        assertEquals("Verify first", AuthUxCopy.primaryLoginLabel(state))
        assertFalse(AuthUxCopy.canAttemptLogin(state))
        assertTrue(AuthUxCopy.canAttemptResend(state))
        assertTrue(AuthUxCopy.verificationGuidance(state).contains("owner@example.com"))
    }

    @Test
    fun verifiedEmailAllowsLoginCopyButDoesNotPretendSessionIsActive() {
        val state = AuthUiState(
            email = "owner@example.com",
            password = "StrongPass123!",
            emailVerified = true,
            authenticated = false
        )

        assertEquals("Email verified", AuthUxCopy.heroTitle(state))
        assertEquals("Ready to log in", AuthUxCopy.sessionLabel(state))
        assertEquals("Login now", AuthUxCopy.primaryLoginLabel(state))
        assertTrue(AuthUxCopy.canAttemptLogin(state))
    }

    @Test
    fun authenticatedStateShowsConnectedCopy() {
        val state = AuthUiState(
            email = "owner@example.com",
            authenticated = true,
            emailVerified = true,
            userLabel = "Owner • owner@example.com • OWNER"
        )

        assertEquals("You are connected", AuthUxCopy.heroTitle(state))
        assertEquals("Session active", AuthUxCopy.sessionLabel(state))
    }

    @Test
    fun verifyButtonNeedsEmailAndCode() {
        assertFalse(AuthUxCopy.canAttemptVerify(AuthUiState(email = "owner@example.com")))
        assertFalse(AuthUxCopy.canAttemptVerify(AuthUiState(verificationCode = "123456")))
        assertTrue(AuthUxCopy.canAttemptVerify(AuthUiState(email = "owner@example.com", verificationCode = "123456")))
    }
}
