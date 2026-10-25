package com.aixion.controltower.notifications

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
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
            PushDeviceRegistrationWorker.enqueue(this)
            Log.i(TAG, "FCM token refreshed; backend registration queued")
        } catch (_: Exception) {
            // The encrypted token remains available for a later authenticated
            // registration attempt if scheduling fails. Never log the token.
            Log.w(TAG, "FCM token refresh could not be stored or queued; registration must retry")
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
        // Push copy can include private project/task text. The preview policy is
        // intentionally generic while the detail remains available after unlock.
        val preview = ApprovalNotificationPreview.fromUntrustedPush(
            remoteTitle = message.notification?.title ?: message.data["title"],
            remoteBody = message.notification?.body ?: message.data["body"]
        )

        showNotification(
            title = preview.title,
            body = preview.body,
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

        // PendingIntent identity uses Intent.filterEquals(), which includes data URI.
        // Do not reduce an opaque server ID to String.hashCode(): distinct approval
        // notifications can collide and then retarget one another's tap action.
        val stableNotificationId = notificationId ?: entityId
        intent.data = Uri.Builder()
            .scheme(NOTIFICATION_URI_SCHEME)
            .authority(NOTIFICATION_URI_AUTHORITY)
            .appendPath(stableNotificationId)
            .build()
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
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

        // Use the exact opaque ID as the NotificationManager tag. The integer ID
        // is constant; tag + ID together preserve per-notification replacement
        // without a lossy 32-bit hash.
        manager.notify(notificationTag(stableNotificationId), NOTIFICATION_ID, notification)
    }

    private fun notificationTag(notificationId: String): String =
        "$NOTIFICATION_TAG_PREFIX$notificationId"

    companion object {
        private const val TAG = "AixionFCM"
        private const val PUSH_PREFERENCES = "aixion_push"
        private const val PENDING_FCM_TOKEN = "pending_fcm_token"
        private const val PUSH_TOKEN_KEY_ALIAS = "com.aixion.controltower.push.token.v1"
        private const val CHANNEL_ID = ApprovalNotificationChannel.ID
        private const val NOTIFICATION_URI_SCHEME = "aixion-control-tower"
        private const val NOTIFICATION_URI_AUTHORITY = "approval-notification"
        private const val NOTIFICATION_TAG_PREFIX = "aixion-approval:"
        private const val NOTIFICATION_ID = 1
    }
}
