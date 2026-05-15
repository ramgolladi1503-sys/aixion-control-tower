package com.aixion.controltower.navigation

object AuthSessionGate {
    fun redirectRoute(
        currentRoute: String?,
        authenticated: Boolean,
        sessionChecked: Boolean
    ): String? {
        val route = currentRoute ?: Route.Auth.value
        val protectedRoute = route != Route.Auth.value

        if (!sessionChecked && protectedRoute) {
            return Route.Auth.value
        }

        if (!sessionChecked) {
            return null
        }

        if (authenticated && route == Route.Auth.value) {
            return Route.Home.value
        }

        if (!authenticated && protectedRoute) {
            return Route.Auth.value
        }

        return null
    }

    fun shouldShowMainShell(
        authenticated: Boolean,
        sessionChecked: Boolean,
        currentRoute: String?
    ): Boolean {
        return authenticated && sessionChecked && currentRoute != Route.Auth.value
    }

    fun canOpenProtectedRoute(authenticated: Boolean, sessionChecked: Boolean): Boolean {
        return authenticated && sessionChecked
    }
}
