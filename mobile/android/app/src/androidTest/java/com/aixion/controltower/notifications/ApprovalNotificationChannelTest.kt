package com.aixion.controltower.notifications

import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.aixion.controltower.ControlTowerApplication
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ApprovalNotificationChannelTest {
    @Test
    fun appCreatesManifestDefaultChannelBeforeBackgroundPushDisplay() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val appInfo = context.packageManager.getApplicationInfo(
            context.packageName,
            PackageManager.GET_META_DATA
        )
        val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

        assertTrue(context.applicationContext is ControlTowerApplication)
        assertEquals(
            ApprovalNotificationChannel.ID,
            appInfo.metaData?.getString(DEFAULT_CHANNEL_METADATA)
        )
        val channel = manager.getNotificationChannel(ApprovalNotificationChannel.ID)
        assertNotNull("Application startup must create the FCM default channel", channel)
    }

    private companion object {
        const val DEFAULT_CHANNEL_METADATA =
            "com.google.firebase.messaging.default_notification_channel_id"
    }
}
