package com.aixion.controltower.notifications

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import com.aixion.controltower.MainActivity
import com.aixion.controltower.R
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

class ControlTowerMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        super.onNewToken(token)
        getSharedPreferences("aixion_push", MODE_PRIVATE)
            .edit()
            .putString("pending_fcm_token", token)
            .apply()
        Log.i(TAG, "FCM token refreshed and stored for backend registration")
    }

    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)
        val entityType = message.data["entity_type"]
        val entityId = message.data["entity_id"]
        val notificationId = message.data["notification_id"]
        Log.i(TAG, "Push received entity=$entityType id=$entityId")

        if (entityType.isNullOrBlank() || entityId.isNullOrBlank()) {
            Log.w(TAG, "Push ignored because entity_type/entity_id is missing")
            return
        }

        showNotification(
            title = message.notification?.title ?: message.data["title"] ?: "Aixion update",
            body = message.notification?.body ?: message.data["body"] ?: "Open Aixion Control Tower",
            entityType = entityType,
            entityId = entityId,
            notificationId = notificationId
        )
    }

    private fun showNotification(
        title: String,
        body: String,
        entityType: String,
        entityId: String,
        notificationId: String?
    ) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        ensureChannel(manager)

        val intent = Intent(this, MainActivity::class.java)
            .setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putNotificationDeepLinkExtras(
                entityType = entityType,
                entityId = entityId,
                notificationId = notificationId
            )

        val requestCode = (notificationId ?: entityId).hashCode()
        val pendingIntent = PendingIntent.getActivity(
            this,
            requestCode,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_aixion)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .build()

        manager.notify(requestCode, notification)
    }

    private fun ensureChannel(manager: NotificationManager) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val existing = manager.getNotificationChannel(CHANNEL_ID)
        if (existing != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Aixion approvals and agent tasks",
            NotificationManager.IMPORTANCE_HIGH
        ).apply {
            description = "Notifications for approvals, agent tasks, and worker results"
        }
        manager.createNotificationChannel(channel)
    }

    companion object {
        private const val TAG = "AixionFCM"
        private const val CHANNEL_ID = "aixion_agent_tasks"
    }
}
