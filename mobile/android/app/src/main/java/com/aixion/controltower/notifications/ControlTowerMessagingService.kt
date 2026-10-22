package com.aixion.controltower.notifications

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.app.NotificationCompat
import com.aixion.controltower.MainActivity
import com.aixion.controltower.R
import com.aixion.controltower.core.auth.SecureStringPreferences
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

class ControlTowerMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        super.onNewToken(token)
        try {
            SecureStringPreferences.put(
                this,
                PUSH_PREFERENCES,
                PENDING_FCM_TOKEN,
                PUSH_TOKEN_KEY_ALIAS,
                token
            )
            Log.i(TAG, "FCM token refreshed and stored securely for backend registration")
        } catch (_: Exception) {
            // The app can retrieve the current token from Firebase on its next
            // authenticated registration attempt; never log the token or fail open.
            Log.w(TAG, "FCM token could not be stored securely; registration must retry")
        }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        super.onMessageReceived(message)
        val entityType = message.data["entity_type"]
        val entityId = message.data["entity_id"]
        val notificationId = message.data["notification_id"]
        // Entity identifiers are useful for app routing but should not be copied
        // into Android logcat, where routine diagnostics can expose private work.
        Log.i(TAG, "Push received")

        val approvalLink = NotificationDeepLink.fromPayload(
            entityType = entityType,
            entityId = entityId,
            notificationId = notificationId
        )
        if (!approvalLink.isApprovalRequest()) {
            Log.i(TAG, "Non-approval push ignored by release-one client")
            return
        }
        val approvedLink = approvalLink as NotificationDeepLink.ApprovalRequest

        showNotification(
            title = message.notification?.title ?: message.data["title"] ?: "Aixion update",
            body = message.notification?.body ?: message.data["body"] ?: "Open Aixion Control Tower",
            entityType = "approval_request",
            entityId = approvedLink.entityId,
            notificationId = approvedLink.notificationId
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
        ApprovalNotificationChannel.ensureCreated(this)

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

    companion object {
        private const val TAG = "AixionFCM"
        private const val PUSH_PREFERENCES = "aixion_push"
        private const val PENDING_FCM_TOKEN = "pending_fcm_token"
        private const val PUSH_TOKEN_KEY_ALIAS = "com.aixion.controltower.push.token.v1"
        private const val CHANNEL_ID = ApprovalNotificationChannel.ID
    }
}
