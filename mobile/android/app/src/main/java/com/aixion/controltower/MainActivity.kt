package com.aixion.controltower

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import com.aixion.controltower.notifications.NotificationDeepLink

class MainActivity : ComponentActivity() {
    private val notificationDeepLink = mutableStateOf<NotificationDeepLink?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        notificationDeepLink.value = NotificationDeepLink.fromIntent(intent)
        setContent {
            ControlTowerApp(notificationDeepLink = notificationDeepLink.value)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        notificationDeepLink.value = NotificationDeepLink.fromIntent(intent)
    }
}
