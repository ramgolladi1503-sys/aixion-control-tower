package com.aixion.controltower.feature.auth

import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.SemanticsMatcher
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class NotificationPermissionAnnouncementsTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun deniedPermissionShowsOneActionableAnnouncementAndHeading() {
        composeRule.setContent {
            ControlTowerTheme {
                NotificationStatusContent(
                    permissionState = NotificationPermissionState.DENIED,
                    registrationState = PushRegistrationUiState.PERMISSION_REQUIRED
                )
            }
        }

        val heading = composeRule.onNodeWithText("Approval notifications")
        assertTrue(heading.fetchSemanticsNode().config.contains(SemanticsProperties.Heading))
        val announcement = composeRule.onNodeWithText("Android notifications are off.")
        assertEquals(
            LiveRegionMode.Polite,
            announcement.fetchSemanticsNode().config[SemanticsProperties.LiveRegion]
        )
        composeRule.onNodeWithText("Allow notifications to register this device for approval alerts.").assertExists()
        composeRule.onAllNodes(SemanticsMatcher.keyIsDefined(SemanticsProperties.LiveRegion), useUnmergedTree = true)
            .assertCountEquals(1)
    }

    @Test
    fun deniedPermissionRetainsCleanupDetailWithoutAnotherLiveAnnouncement() {
        composeRule.setContent {
            ControlTowerTheme {
                NotificationStatusContent(
                    permissionState = NotificationPermissionState.DENIED,
                    registrationState = PushRegistrationUiState.PERMISSION_REQUIRED_CLEANUP_PENDING
                )
            }
        }

        composeRule.onNodeWithText("Android notifications are off.").assertExists()
        composeRule.onNodeWithText(
            "Allow notifications for approval alerts. Server cleanup is pending and will retry when the owning account is available."
        ).assertExists()
        composeRule.onAllNodes(SemanticsMatcher.keyIsDefined(SemanticsProperties.LiveRegion), useUnmergedTree = true)
            .assertCountEquals(1)
    }

    @Test
    fun firebaseConfigurationFailureIsClearAndDoesNotClaimDelivery() {
        composeRule.setContent {
            ControlTowerTheme {
                NotificationStatusContent(
                    permissionState = NotificationPermissionState.GRANTED,
                    registrationState = PushRegistrationUiState.FIREBASE_NOT_CONFIGURED
                )
            }
        }

        composeRule.onNodeWithText("Push alerts are unavailable in this app build.").assertExists()
        composeRule.onNodeWithText("Firebase is not configured for this app build.").assertExists()
        composeRule.onAllNodes(SemanticsMatcher.keyIsDefined(SemanticsProperties.LiveRegion), useUnmergedTree = true)
            .assertCountEquals(1)
    }

    @Test
    fun serverRegistrationDoesNotClaimThatPushDeliveryIsWorking() {
        composeRule.setContent {
            ControlTowerTheme {
                NotificationStatusContent(
                    permissionState = NotificationPermissionState.GRANTED,
                    registrationState = PushRegistrationUiState.REGISTERED
                )
            }
        }

        composeRule.onNodeWithText("Device registered with the server.").assertExists()
        composeRule.onNodeWithText("This confirms registration only, not delivery of a push alert.", substring = true).assertExists()
        composeRule.onAllNodes(SemanticsMatcher.keyIsDefined(SemanticsProperties.LiveRegion), useUnmergedTree = true)
            .assertCountEquals(1)
    }

    @Test
    fun backendRegistrationFailureKeepsRetryGuidanceAsSupportingDetail() {
        composeRule.setContent {
            ControlTowerTheme {
                NotificationStatusContent(
                    permissionState = NotificationPermissionState.GRANTED,
                    registrationState = PushRegistrationUiState.BACKEND_UNAVAILABLE
                )
            }
        }

        composeRule.onNodeWithText("This device could not be registered for approval alerts.").assertExists()
        composeRule.onNodeWithText(
            "Registration could not be completed. Reopen Account to retry.",
            substring = true
        ).assertExists()
        composeRule.onAllNodes(SemanticsMatcher.keyIsDefined(SemanticsProperties.LiveRegion), useUnmergedTree = true)
            .assertCountEquals(1)
    }
}
