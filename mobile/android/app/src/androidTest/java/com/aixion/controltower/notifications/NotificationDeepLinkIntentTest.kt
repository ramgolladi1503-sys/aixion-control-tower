package com.aixion.controltower.notifications

import android.content.Intent
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class NotificationDeepLinkIntentTest {
    @Test
    fun coldStartNotificationIntentOpensApprovalReference() {
        val launchIntent = Intent().putNotificationDeepLinkExtras(
            entityType = "approval_request",
            entityId = "approval_cold_start",
            notificationId = "notification_cold_start"
        )

        assertEquals(
            NotificationDeepLink.ApprovalRequest("approval_cold_start", "notification_cold_start"),
            NotificationDeepLink.fromIntent(launchIntent)
        )
    }

    @Test
    fun newIntentForAlreadyRunningAppDecodesTheNewApprovalReference() {
        val onNewIntent = Intent().putNotificationDeepLinkExtras(
            entityType = "approval_request",
            entityId = "approval_hot_start",
            notificationId = "notification_hot_start"
        )

        assertEquals(
            NotificationDeepLink.ApprovalRequest("approval_hot_start", "notification_hot_start"),
            NotificationDeepLink.fromIntent(onNewIntent)
        )
    }
}
