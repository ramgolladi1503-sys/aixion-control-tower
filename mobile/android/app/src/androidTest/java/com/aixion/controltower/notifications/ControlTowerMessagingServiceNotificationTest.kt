package com.aixion.controltower.notifications

import android.app.Activity
import android.app.ActivityOptions
import android.app.Instrumentation
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
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
        val manager = prepareNotificationDelivery(instrumentation, context)
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

            val posted = awaitActiveNotifications(manager) {
                it.singleOrNull { notification ->
                    notification.tag == notificationTag && notification.id == NOTIFICATION_ID
                }
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

            val foregroundActivity = instrumentation.startActivitySync(
                Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )

            val activityMonitor = Instrumentation.ActivityMonitor(
                MainActivity::class.java.name,
                null,
                false
            )
            instrumentation.addMonitor(activityMonitor)
            var launchedActivity: Activity? = null
            try {
                assertNotNull("Approval notification must have a tap intent", notification.contentIntent)
                requireNotNull(notification.contentIntent).sendFromInstrumentation(context)
                val activity = activityMonitor.waitForActivityWithTimeout(ACTIVITY_START_TIMEOUT_MS)
                assertNotNull("Notification tap should launch MainActivity", activity)
                val startedActivity = activity as MainActivity
                launchedActivity = startedActivity
                val resolvedLink = NotificationDeepLink.fromIntent(startedActivity.intent)
                assertEquals(
                    NotificationDeepLink.ApprovalRequest(approvalId, notificationId),
                    resolvedLink
                )

                val expectedLink = NotificationDeepLink.ApprovalRequest(approvalId, notificationId)
                assertEquals(expectedLink, mainActivityNotificationState(startedActivity).current.link)

                // Exercise MainActivity's real save/restore path after the notification tap.
                // This does not simulate Android process death or prove network/auth behavior.
                instrumentation.runOnMainSync { startedActivity.recreate() }
                val recreatedActivity = awaitResumedMainActivityDifferentFrom(
                    instrumentation = instrumentation,
                    previousActivity = startedActivity
                )
                launchedActivity = recreatedActivity
                assertEquals(expectedLink, NotificationDeepLink.fromIntent(recreatedActivity.intent))
                val restoredState = mainActivityNotificationState(recreatedActivity)
                assertEquals(expectedLink, restoredState.current.link)
                assertEquals(expectedLink, restoredState.pendingEvents().single().link)
            } finally {
                instrumentation.runOnMainSync {
                    launchedActivity?.finish()
                    foregroundActivity.finish()
                }
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
        val manager = prepareNotificationDelivery(instrumentation, context)
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

            val active = awaitActiveNotifications(manager) { notifications ->
                notifications
                    .filter { it.tag in notificationIds.map(::notificationTag) }
                    .associateBy { it.tag }
                    .takeIf { it.size == 2 }
            }
            assertEquals("Distinct approvals must remain independently visible", 2, active.size)
            val first = requireNotNull(active[notificationTag(notificationIds[0])]).notification.contentIntent
            val second = requireNotNull(active[notificationTag(notificationIds[1])]).notification.contentIntent
            assertNotNull(first)
            assertNotNull(second)
            assertNotEquals("Hash-colliding approvals must not share a tap target", first, second)

            val foregroundActivity = instrumentation.startActivitySync(
                Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )

            val activityMonitor = Instrumentation.ActivityMonitor(
                MainActivity::class.java.name,
                null,
                false
            )
            instrumentation.addMonitor(activityMonitor)
            val launchedActivities = mutableListOf<Activity>()
            try {
                requireNotNull(first).sendFromInstrumentation(context)
                val firstActivity = activityMonitor.waitForActivityWithTimeout(ACTIVITY_START_TIMEOUT_MS)
                assertNotNull("First colliding notification should launch MainActivity", firstActivity)
                val mainActivity = requireNotNull(firstActivity) as MainActivity
                launchedActivities += mainActivity
                assertEquals(
                    NotificationDeepLink.ApprovalRequest(approvalIds[0], notificationIds[0]),
                    NotificationDeepLink.fromIntent(mainActivity.intent)
                )

                requireNotNull(second).sendFromInstrumentation(context)
                val secondActivity = activityMonitor.waitForActivityWithTimeout(ACTIVITY_START_TIMEOUT_MS)
                assertNotNull("Second colliding notification should launch MainActivity", secondActivity)
                val secondMainActivity = requireNotNull(secondActivity) as MainActivity
                launchedActivities += secondMainActivity
                assertEquals(
                    "Second colliding notification should deliver its own approval reference",
                    NotificationDeepLink.ApprovalRequest(approvalIds[1], notificationIds[1]),
                    NotificationDeepLink.fromIntent(secondMainActivity.intent)
                )
            } finally {
                instrumentation.runOnMainSync {
                    launchedActivities.forEach(Activity::finish)
                    foregroundActivity.finish()
                }
                instrumentation.removeMonitor(activityMonitor)
            }

            // Redelivery is idempotent for the same notification: replace its card,
            // while keeping the other colliding notification and its target intact.
            deliver(1)
            val afterRedelivery = awaitActiveNotifications(manager) { notifications ->
                notifications
                    .filter { it.tag in notificationIds.map(::notificationTag) }
                    .associateBy { it.tag }
                    .takeIf { it.size == 2 }
            }
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

    /** Establishes and asserts the Android prerequisites before exercising delivery. */
    private fun prepareNotificationDelivery(
        instrumentation: Instrumentation,
        context: Context
    ): NotificationManager {
        grantNotificationPermissionIfNeeded(instrumentation, context)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            assertEquals(
                "POST_NOTIFICATIONS must be granted for delivery assertions",
                PackageManager.PERMISSION_GRANTED,
                context.checkSelfPermission("android.permission.POST_NOTIFICATIONS")
            )
        }

        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        assertTrue("App-level notifications must be enabled for delivery assertions", manager.areNotificationsEnabled())
        ApprovalNotificationChannel.ensureCreated(context)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = manager.getNotificationChannel(ApprovalNotificationChannel.ID)
            assertNotNull("Approval notification channel must exist", channel)
            assertTrue(
                "Approval notification channel must not be blocked",
                requireNotNull(channel).importance != NotificationManager.IMPORTANCE_NONE
            )
        }
        return manager
    }

    /**
     * NotificationManager exposes notifications after system-server processes the
     * notify binder call. Poll briefly for that observable state, retaining the
     * full assertion when no notification appears within the bound.
     */
    private fun <T : Any> awaitActiveNotifications(
        manager: NotificationManager,
        selector: (Array<android.service.notification.StatusBarNotification>) -> T?
    ): T {
        val deadline = SystemClock.uptimeMillis() + NOTIFICATION_VISIBLE_TIMEOUT_MS
        do {
            selector(manager.activeNotifications)?.let { return it }
            SystemClock.sleep(NOTIFICATION_POLL_INTERVAL_MS)
        } while (SystemClock.uptimeMillis() < deadline)
        throw AssertionError("Expected notification state was not visible within ${NOTIFICATION_VISIBLE_TIMEOUT_MS}ms")
    }

    /** The test process must explicitly opt in to API 34+ background launches. */
    private fun PendingIntent.sendFromInstrumentation(context: Context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            val options = ActivityOptions.makeBasic()
                .setPendingIntentBackgroundActivityStartMode(
                    ActivityOptions.MODE_BACKGROUND_ACTIVITY_START_ALLOWED
                )
                .toBundle()
            send(context, 0, null, null, null, null, options)
        } else {
            send()
        }
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

    private fun mainActivityNotificationState(activity: MainActivity): NotificationDeepLinkEventState =
        MainActivity::class.java
            .getDeclaredField("notificationEventState")
            .apply { isAccessible = true }
            .get(activity) as NotificationDeepLinkEventState

    private fun awaitResumedMainActivityDifferentFrom(
        instrumentation: Instrumentation,
        previousActivity: MainActivity
    ): MainActivity {
        val deadline = SystemClock.uptimeMillis() + ACTIVITY_START_TIMEOUT_MS
        while (SystemClock.uptimeMillis() < deadline) {
            var resumedActivity: MainActivity? = null
            instrumentation.runOnMainSync {
                resumedActivity = ActivityLifecycleMonitorRegistry.getInstance()
                    .getActivitiesInStage(Stage.RESUMED)
                    .filterIsInstance<MainActivity>()
                    .firstOrNull { it !== previousActivity }
            }
            resumedActivity?.let { return it }
            Thread.sleep(50)
        }
        throw AssertionError("MainActivity was not recreated and resumed after the notification tap")
    }

    private companion object {
        const val ACTIVITY_START_TIMEOUT_MS = 5_000L
        const val NOTIFICATION_VISIBLE_TIMEOUT_MS = 2_000L
        const val NOTIFICATION_POLL_INTERVAL_MS = 50L
        const val NOTIFICATION_ID = 1

        fun notificationTag(notificationId: String): String = "aixion-approval:$notificationId"
    }
}
