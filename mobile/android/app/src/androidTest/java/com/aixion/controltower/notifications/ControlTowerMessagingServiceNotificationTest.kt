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
        val requestCode = notificationId.hashCode()
        manager.cancel(requestCode)

        try {
            val message = RemoteMessage.Builder("approval@aixion")
                .addData("entity_type", "approval_request")
                .addData("entity_id", approvalId)
                .addData("notification_id", notificationId)
                .addData("title", "Private client project alpha")
                .addData("body", "Run the confidential production migration")
                .build()

            createMessagingService(context).onMessageReceived(message)

            val posted = manager.activeNotifications.singleOrNull { it.id == requestCode }
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
            manager.cancel(requestCode)
        }
    }

    @Test
    fun invalidAndNonApprovalPushesDoNotPostNotifications() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val testIds = listOf("not-an-approval", "invalid-approval-id")
        val requestCodes = testIds.map(String::hashCode)
        requestCodes.forEach(manager::cancel)

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

            val postedIds = manager.activeNotifications.map { it.id }
            assertTrue("Non-approval entity must be ignored", requestCodes[0] !in postedIds)
            assertTrue("Malformed approval reference must fail closed", requestCodes[1] !in postedIds)
        } finally {
            requestCodes.forEach(manager::cancel)
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
    }
}
