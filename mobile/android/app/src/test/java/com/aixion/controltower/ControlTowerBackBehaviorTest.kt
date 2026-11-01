package com.aixion.controltower

import com.aixion.controltower.navigation.Route
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ControlTowerBackBehaviorTest {
    @Test
    fun homeBackPromptsOnlyForAnActiveVerifiedSessionWhenDialogIsClosed() {
        assertTrue(
            shouldShowLogoutConfirmationOnBack(
                authenticated = true,
                sessionChecked = true,
                currentRoute = Route.Home.value,
                dialogVisible = false
            )
        )
    }

    @Test
    fun backDoesNotReopenLogoutDialogOrInterceptOtherRoutes() {
        assertFalse(
            shouldShowLogoutConfirmationOnBack(
                authenticated = true,
                sessionChecked = true,
                currentRoute = Route.Home.value,
                dialogVisible = true
            )
        )
        assertFalse(
            shouldShowLogoutConfirmationOnBack(
                authenticated = true,
                sessionChecked = true,
                currentRoute = Route.Inbox.value,
                dialogVisible = false
            )
        )
        assertFalse(
            shouldShowLogoutConfirmationOnBack(
                authenticated = false,
                sessionChecked = true,
                currentRoute = Route.Home.value,
                dialogVisible = false
            )
        )
        assertFalse(
            shouldShowLogoutConfirmationOnBack(
                authenticated = true,
                sessionChecked = false,
                currentRoute = Route.Home.value,
                dialogVisible = false
            )
        )
    }
}
