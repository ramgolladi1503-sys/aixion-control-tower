package com.aixion.controltower.feature.auth

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NotificationPermissionPanelTest {
    @Test
    fun deniedPermissionAndRequiredRegistrationExplainThatAlertsAreBlocked() {
        assertEquals(
            NotificationPanelMessage(
                headline = "Android notifications are off.",
                supportingDetail = "Allow notifications to register this device for approval alerts."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.PERMISSION_REQUIRED
            )
        )
    }

    @Test
    fun grantedPermissionAndRegistrationDoNotClaimPushDelivery() {
        assertEquals(
            NotificationPanelMessage(
                headline = "Device registered with the server.",
                supportingDetail =
                    "Android allows notifications on this device. This confirms registration only, not delivery of a push alert."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.GRANTED,
                PushRegistrationUiState.REGISTERED
            )
        )
    }

    @Test
    fun olderAndroidStateDoesNotClaimPermissionWasGranted() {
        assertEquals(
            NotificationPanelMessage(
                headline = "Device registered with the server.",
                supportingDetail =
                    "This Android version does not require a notification permission prompt. This confirms registration only, not delivery of a push alert."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.NOT_REQUIRED,
                PushRegistrationUiState.REGISTERED
            )
        )
    }

    @Test
    fun missingFirebaseConfigurationIsNotPresentedAsSuccess() {
        assertEquals(
            NotificationPanelMessage(
                headline = "Push alerts are unavailable in this app build.",
                supportingDetail = "Firebase is not configured for this app build."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.GRANTED,
                PushRegistrationUiState.FIREBASE_NOT_CONFIGURED
            )
        )
    }

    @Test
    fun backendFailureOffersRetryPath() {
        assertEquals(
            NotificationPanelMessage(
                headline = "This device could not be registered for approval alerts.",
                supportingDetail = "Registration could not be completed. Reopen Account to retry."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.GRANTED,
                PushRegistrationUiState.BACKEND_UNAVAILABLE
            )
        )
    }

    @Test
    fun oldDeviceCleanupFailureIsNotHiddenAsFullyRegistered() {
        val message = NotificationPanelCopy.message(
            NotificationPermissionState.GRANTED,
            PushRegistrationUiState.REGISTERED_CLEANUP_PENDING
        )

        assertEquals("Device registered with the server.", message.headline)
        assertEquals(
            "An older device registration could not be removed yet. Cleanup will retry when the owning account is available. This does not confirm push delivery.",
            message.supportingDetail
        )
    }

    @Test
    fun revokedPermissionShowsPendingServerCleanup() {
        assertEquals(
            NotificationPanelMessage(
                headline = "Android notifications are off.",
                supportingDetail =
                    "Allow notifications for approval alerts. Server cleanup is pending and will retry when the owning account is available."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.PERMISSION_REQUIRED_CLEANUP_PENDING
            )
        )
    }

    @Test
    fun checkingStateRetainsPermissionContext() {
        assertEquals(
            NotificationPanelMessage(
                headline = "Checking approval alert setup…",
                supportingDetail = "Android allows notifications on this device."
            ),
            NotificationPanelCopy.message(
                NotificationPermissionState.GRANTED,
                PushRegistrationUiState.CHECKING
            )
        )
    }

    @Test
    fun permissionActionAppearsOnlyWhenFirebaseIsConfiguredAndPermissionIsTheBlocker() {
        assertTrue(
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.PERMISSION_REQUIRED,
                firebaseConfigured = true
            )
        )
        assertFalse(
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.FIREBASE_NOT_CONFIGURED,
                firebaseConfigured = false
            )
        )
        assertFalse(
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.DENIED,
                PushRegistrationUiState.CHECKING,
                firebaseConfigured = true
            )
        )
        assertFalse(
            shouldShowNotificationPermissionAction(
                NotificationPermissionState.GRANTED,
                PushRegistrationUiState.PERMISSION_REQUIRED,
                firebaseConfigured = true
            )
        )
    }
}
