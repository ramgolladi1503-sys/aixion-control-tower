package com.aixion.controltower.feature.auth

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthScreenPresentationTest {
    @Test
    fun accountIdentityRemovesRepeatedPartsAndPreservesTheDistinctRole() {
        assertEquals(
            "owner@example.com • OWNER",
            formatAccountIdentityLabel("owner@example.com • owner@example.com • OWNER")
        )
        assertEquals(
            "Madhuri • madhuri@example.com • OWNER",
            formatAccountIdentityLabel("Madhuri • madhuri@example.com • OWNER")
        )
        assertNull(formatAccountIdentityLabel("  •  "))
    }

    @Test
    fun onlyKnownAuthFailuresAreAssertiveErrors() {
        assertTrue(isAuthErrorMessage("Login failed: invalid credentials"))
        assertTrue(isAuthErrorMessage("Registration failed: email already exists"))
        assertTrue(isAuthErrorMessage("Session expired or invalid. Sign in again."))
        assertFalse(isAuthErrorMessage("Logged in."))
        assertFalse(isAuthErrorMessage("Verification code sent."))
        assertFalse(isAuthErrorMessage(null))
    }
}
