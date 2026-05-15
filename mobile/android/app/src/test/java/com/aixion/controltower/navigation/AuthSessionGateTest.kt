package com.aixion.controltower.navigation

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AuthSessionGateTest {
    @Test
    fun freshInstallStaysOnAuthRouteAfterSessionCheck() {
        val target = AuthSessionGate.redirectRoute(
            currentRoute = Route.Auth.value,
            authenticated = false,
            sessionChecked = true
        )

        assertNull(target)
        assertFalse(AuthSessionGate.shouldShowMainShell(false, true, Route.Auth.value))
    }

    @Test
    fun validSessionMovesAuthRouteToHome() {
        val target = AuthSessionGate.redirectRoute(
            currentRoute = Route.Auth.value,
            authenticated = true,
            sessionChecked = true
        )

        assertEquals(Route.Home.value, target)
    }

    @Test
    fun unauthenticatedProtectedRouteReturnsToAuth() {
        val target = AuthSessionGate.redirectRoute(
            currentRoute = Route.Home.value,
            authenticated = false,
            sessionChecked = true
        )

        assertEquals(Route.Auth.value, target)
    }

    @Test
    fun uncheckedSavedSessionCannotOpenProtectedRoute() {
        val target = AuthSessionGate.redirectRoute(
            currentRoute = Route.Inbox.value,
            authenticated = false,
            sessionChecked = false
        )

        assertEquals(Route.Auth.value, target)
        assertFalse(AuthSessionGate.canOpenProtectedRoute(false, false))
    }

    @Test
    fun authenticatedProtectedRouteShowsMainShell() {
        assertTrue(AuthSessionGate.shouldShowMainShell(true, true, Route.Home.value))
        assertTrue(AuthSessionGate.canOpenProtectedRoute(true, true))
    }

    @Test
    fun accountScreenIsProtectedAfterLoginOnly() {
        assertEquals(
            Route.Auth.value,
            AuthSessionGate.redirectRoute(Route.Account.value, authenticated = false, sessionChecked = true)
        )
        assertNull(AuthSessionGate.redirectRoute(Route.Account.value, authenticated = true, sessionChecked = true))
    }
}
