package com.aixion.controltower.feature.auth

import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import com.aixion.controltower.core.ui.theme.ControlTowerTheme
import com.aixion.controltower.core.ui.theme.LocalTowerPalette
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class NotificationPermissionAnnouncementsTest {
    @get:Rule
    val composeRule = createComposeRule()

    @Test
    fun permissionAndRegistrationStatusesArePoliteLiveRegions() {
        val permissionStatus =
            "Android notifications are off. Push notifications cannot appear until you allow them."
        val registrationStatus =
            "Notifications are off, and server cleanup is pending. Cleanup will retry when the owning account is available."

        composeRule.setContent {
            ControlTowerTheme {
                val statusColor = LocalTowerPalette.current.inkMuted
                NotificationStatusAnnouncement(permissionStatus, statusColor)
                NotificationStatusAnnouncement(registrationStatus, statusColor)
            }
        }

        listOf(permissionStatus, registrationStatus).forEach { message ->
            val semantics = composeRule.onNodeWithText(message).fetchSemanticsNode().config
            assertEquals(LiveRegionMode.Polite, semantics[SemanticsProperties.LiveRegion])
        }
    }
}
