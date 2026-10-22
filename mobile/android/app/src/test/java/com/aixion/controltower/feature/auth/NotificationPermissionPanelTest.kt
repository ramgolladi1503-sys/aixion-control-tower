package com.aixion.controltower.feature.auth

import org.junit.Assert.assertEquals
import org.junit.Test

class NotificationPermissionPanelTest {
    @Test
    fun deniedStateClearlyExplainsPushIsBlocked() {
        assertEquals("ALERTS OFF", NotificationPermissionCopy.badge(NotificationPermissionState.DENIED))
        assertEquals(
            "Approval alerts are off. Android will not show push notifications until you allow them.",
            NotificationPermissionCopy.status(NotificationPermissionState.DENIED)
        )
    }

    @Test
    fun grantedStateConfirmsDevicePermission() {
        assertEquals("ALERTS ENABLED", NotificationPermissionCopy.badge(NotificationPermissionState.GRANTED))
        assertEquals(
            "Approval alerts are enabled on this device.",
            NotificationPermissionCopy.status(NotificationPermissionState.GRANTED)
        )
    }

    @Test
    fun olderAndroidStateDoesNotClaimPermissionWasGranted() {
        assertEquals("SYSTEM MANAGED", NotificationPermissionCopy.badge(NotificationPermissionState.NOT_REQUIRED))
        assertEquals(
            "This Android version does not require a notification permission prompt.",
            NotificationPermissionCopy.status(NotificationPermissionState.NOT_REQUIRED)
        )
    }

    @Test
    fun successfulRegistrationIsReportedExplicitly() {
        assertEquals(
            "This device is registered to receive approval alerts.",
            PushRegistrationCopy.status(PushRegistrationUiState.REGISTERED)
        )
    }

    @Test
    fun missingFirebaseConfigurationIsNotPresentedAsSuccess() {
        assertEquals(
            "Firebase is not configured for this app build. Push alerts are unavailable.",
            PushRegistrationCopy.status(PushRegistrationUiState.FIREBASE_NOT_CONFIGURED)
        )
    }

    @Test
    fun backendFailureOffersRetryPath() {
        assertEquals(
            "Could not register this device with the server. Reopen Account to retry.",
            PushRegistrationCopy.status(PushRegistrationUiState.BACKEND_UNAVAILABLE)
        )
    }

    @Test
    fun oldDeviceCleanupFailureIsNotHiddenAsFullyRegistered() {
        assertEquals(
            "Alerts are registered, but an older device registration could not be removed yet.",
            PushRegistrationCopy.status(PushRegistrationUiState.REGISTERED_CLEANUP_PENDING)
        )
    }

    @Test
    fun revokedPermissionShowsPendingServerCleanup() {
        assertEquals(
            "Notifications are off, and server cleanup is pending. Cleanup will retry when the owning account is available.",
            PushRegistrationCopy.status(PushRegistrationUiState.PERMISSION_REQUIRED_CLEANUP_PENDING)
        )
    }

    @Test
    fun backgroundRegistrationDoesNotClaimSuccessWithoutNotificationPermission() {
        assertEquals(
            "Allow notifications to register this device for approval alerts.",
            PushRegistrationCopy.status(PushRegistrationUiState.PERMISSION_REQUIRED)
        )
    }

    @Test
    fun permissionActionAppearsOnlyWhenFirebaseIsConfiguredAndPermissionIsTheBlocker() {
        assertEquals(
            true,
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.PERMISSION_REQUIRED,
                firebaseConfigured = true
            )
        )
        assertEquals(
            false,
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.FIREBASE_NOT_CONFIGURED,
                firebaseConfigured = false
            )
        )
        assertEquals(
            false,
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.CHECKING,
                firebaseConfigured = true
            )
        )
        assertEquals(
            false,
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.GRANTED,
                PushRegistrationUiState.PERMISSION_REQUIRED,
                firebaseConfigured = true
            )
        )
    }
}
