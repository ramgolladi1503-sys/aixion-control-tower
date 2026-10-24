package com.aixion.controltower

import android.app.Application
import com.aixion.controltower.notifications.ApprovalNotificationChannel

class ControlTowerApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        ApprovalNotificationChannel.ensureCreated(this)
    }
}
