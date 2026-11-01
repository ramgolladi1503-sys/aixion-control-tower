package com.aixion.controltower.feature.auth

import org.junit.Assert.assertEquals
import org.junit.Test

class LogoutConfirmationCopyTest {
    @Test
    fun logoutConfirmationCopyStaysConsistent() {
        assertEquals("Log out of Control Tower?", LogoutConfirmationCopy.TITLE)
        assertEquals("You will need to sign in again to review approvals.", LogoutConfirmationCopy.MESSAGE)
        assertEquals("Log out", LogoutConfirmationCopy.CONFIRM)
        assertEquals("Stay signed in", LogoutConfirmationCopy.DISMISS)
    }
}
