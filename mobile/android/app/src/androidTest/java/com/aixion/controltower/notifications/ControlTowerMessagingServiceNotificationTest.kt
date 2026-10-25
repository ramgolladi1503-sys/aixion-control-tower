package com.aixion.controltower.notifications

import android.app.Activity
import android.app.Instrumentation
import android.app.NotificationManager
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import android.os.Build
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aixion.controltower.MainActivity
import com.google.firebase.messaging.RemoteMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ControlTowerMessagingServiceNotificationTest {
    @Test
    fun approvalPushPostsPrivatePreviewAndTapOpensExactApprovalReference() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        grantNotificationPermissionIfNeeded(instrumentation, context)
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val approvalId = "approval_device_test_42"
        val notificationId = "notification_device_test_42"
        val notificationTag = notificationTag(notificationId)
        manager.cancel(notificationTag, NOTIFICATION_ID)

        try {
            val message = RemoteMessage.Builder("approval@aixion")
                .addData("entity_type", "approval_request")
                .addData("entity_id", approvalId)
                .addData("notification_id", notificationId)
                .addData("title", "Private client project alpha")
                .addData("body", "Run the confidential production migration")
                .build()

            createMessagingService(context).onMessageReceived(message)

            val posted = manager.activeNotifications.singleOrNull {
                it.tag == notificationTag && it.id == NOTIFICATION_ID
            }
            assertNotNull("Approval push should create a notification", posted)
            val notification = requireNotNull(posted).notification
            assertEquals("Aixion Control Tower", notification.extras.getCharSequence("android.title"))
            assertEquals(
                "Approval needed. Open the app to review.",
                notification.extras.getCharSequence("android.text")
            )
            assertFalse(notification.extras.toString().contains("confidential"))
            assertFalse(notification.extras.toString().contains("Private client"))

            val activityMonitor = Instrumentation.ActivityMonitor(
                MainActivity::class.java.name,
                null,
                false
            )
            instrumentation.addMonitor(activityMonitor)
            try {
                assertNotNull("Approval notification must have a tap intent", notification.contentIntent)
                notification.contentIntent.send()
                val activity = activityMonitor.waitForActivityWithTimeout(ACTIVITY_START_TIMEOUT_MS)
                assertNotNull("Notification tap should launch MainActivity", activity)
                val resolvedLink = NotificationDeepLink.fromIntent((activity as Activity).intent)
                assertEquals(
                    NotificationDeepLink.ApprovalRequest(approvalId, notificationId),
                    resolvedLink
                )
            } finally {
                instrumentation.removeMonitor(activityMonitor)
            }
        } finally {
            manager.cancel(notificationTag, NOTIFICATION_ID)
        }
    }

    @Test
    fun collidingStringHashesKeepDistinctApprovalNotificationsAndPendingIntents() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val context = instrumentation.targetContext
        grantNotificationPermissionIfNeeded(instrumentation, context)
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val notificationIds = listOf("notification_Aa", "notification_BB")
        val approvalIds = listOf("approval_Aa", "approval_BB")
        assertEquals(
            "Test IDs must exercise the Java hash collision",
            notificationIds[0].hashCode(),
            notificationIds[1].hashCode()
        )
        notificationIds.forEach { manager.cancel(notificationTag(it), NOTIFICATION_ID) }

        try {
            val service = createMessagingService(context)
            fun deliver(index: Int) {
                service.onMessageReceived(
                    RemoteMessage.Builder("approval@aixion")
                        .addData("entity_type", "approval_request")
                        .addData("entity_id", approvalIds[index])
                        .addData("notification_id", notificationIds[index])
                        .build()
                )
            }

            deliver(0)
            deliver(1)

            var active = manager.activeNotifications
                .filter { it.tag in notificationIds.map(::notificationTag) }
                .associateBy { it.tag }
            assertEquals("Distinct approvals must remain independently visible", 2, active.size)
            val first = requireNotNull(active[notificationTag(notificationIds[0])]).notification.contentIntent
            val second = requireNotNull(active[notificationTag(notificationIds[1])]).notification.contentIntent
            assertNotNull(first)
            assertNotNull(second)
            assertNotEquals("Hash-colliding approvals must not share a tap target", first, second)

            val activityMonitor = Instrumentation.ActivityMonitor(
                MainActivity::class.java.name,
                null,
                false
            )
            instrumentation.addMonitor(activityMonitor)
            try {
                first.send()
                val activity = activityMonitor.waitForActivityWithTimeout(ACTIVITY_START_TIMEOUT_MS)
                assertNotNull("First colliding notification should launch MainActivity", activity)
                val mainActivity = requireNotNull(activity)
                assertEquals(
                    NotificationDeepLink.ApprovalRequest(approvalIds[0], notificationIds[0]),
                    NotificationDeepLink.fromIntent(mainActivity.intent)
                )

                second.send()
                instrumentation.waitForIdleSync()
                assertEquals(
                    "Second colliding notification should deliver its own approval reference",
                    NotificationDeepLink.ApprovalRequest(approvalIds[1], notificationIds[1]),
                    NotificationDeepLink.fromIntent(mainActivity.intent)
                )
            } finally {
                instrumentation.removeMonitor(activityMonitor)
            }

            // Redelivery is idempotent for the same notification: replace its card,
            // while keeping the other colliding notification and its target intact.
            deliver(1)
            val afterRedelivery = manager.activeNotifications
                .filter { it.tag in notificationIds.map(::notificationTag) }
                .associateBy { it.tag }
            assertEquals("Same-ID redelivery should replace only its card", 2, afterRedelivery.size)
            assertNotEquals(
                "Each active card must retain a distinct tap target",
                requireNotNull(afterRedelivery[notificationTag(notificationIds[0])])
                    .notification.contentIntent,
                requireNotNull(afterRedelivery[notificationTag(notificationIds[1])])
                    .notification.contentIntent
            )
        } finally {
            notificationIds.forEach { manager.cancel(notificationTag(it), NOTIFICATION_ID) }
        }
    }

    @Test
    fun invalidAndNonApprovalPushesDoNotPostNotifications() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val testIds = listOf("not-an-approval", "invalid-approval-id")
        val notificationTags = testIds.map(::notificationTag)
        notificationTags.forEach { manager.cancel(it, NOTIFICATION_ID) }

        try {
            val service = createMessagingService(context)
            service.onMessageReceived(
                RemoteMessage.Builder("approval@aixion")
                    .addData("entity_type", "agent_task")
                    .addData("entity_id", testIds[0])
                    .addData("notification_id", testIds[0])
                    .build()
            )
            service.onMessageReceived(
                RemoteMessage.Builder("approval@aixion")
                    .addData("entity_type", "approval_request")
                    .addData("entity_id", "bad id with spaces")
                    .addData("notification_id", testIds[1])
                    .build()
            )

            val postedTags = manager.activeNotifications.map { it.tag }
            assertTrue("Non-approval entity must be ignored", notificationTags[0] !in postedTags)
            assertTrue("Malformed approval reference must fail closed", notificationTags[1] !in postedTags)
        } finally {
            notificationTags.forEach { manager.cancel(it, NOTIFICATION_ID) }
        }
    }

    private fun grantNotificationPermissionIfNeeded(
        instrumentation: Instrumentation,
        context: Context
    ) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val permission = "android.permission.POST_NOTIFICATIONS"
        if (context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED) return
        instrumentation.uiAutomation.grantRuntimePermission(context.packageName, permission)
    }

    /** Attaches the app context to the real final Android service instance for an isolated callback test. */
    private fun createMessagingService(context: Context): ControlTowerMessagingService {
        val service = ControlTowerMessagingService()
        ContextWrapper::class.java
            .getDeclaredMethod("attachBaseContext", Context::class.java)
            .apply { isAccessible = true }
            .invoke(service, context)
        return service
    }

    private companion object {
        const val ACTIVITY_START_TIMEOUT_MS = 5_000L
        const val NOTIFICATION_ID = 1

        fun notificationTag(notificationId: String): String = "aixion-approval:$notificationId"
    }
}
