package com.aixion.controltower.notifications

import android.util.Log
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
        Log.i(
            TAG,
            "Push received entity=${message.data["entity_type"]} id=${message.data["entity_id"]}"
        )
    }

    companion object {
        private const val TAG = "AixionFCM"
    }
}
