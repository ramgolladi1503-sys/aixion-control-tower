package com.aixion.controltower.feature.auth

import org.junit.Assert.assertEquals
import org.junit.Test

class LogoutConfirmationCopyTest {
    @Test
    fun logoutConfirmationCopyStaysConsistent() {
        assertEquals("Do you wish to logout?", LogoutConfirmationCopy.TITLE)
        assertEquals("Your local session will be cleared and you will return to login.", LogoutConfirmationCopy.MESSAGE)
        assertEquals("Yes", LogoutConfirmationCopy.CONFIRM)
        assertEquals("No", LogoutConfirmationCopy.DISMISS)
    }
}
